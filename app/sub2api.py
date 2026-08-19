from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EMAIL_SEARCH_PATTERN = re.compile(
    r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+",
    re.IGNORECASE,
)
CARD_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]$")
HTTP_401_PATTERN = re.compile(r"(?<!\d)401(?!\d)")
ADMIN_API_KEY_PATTERN = re.compile(r"^admin-[0-9a-f]{64}$", re.IGNORECASE)
DEFAULT_ACCOUNT_CONCURRENCY = 10


class DownloadPayloadError(ValueError):
    """Raised when a downloaded file is not a supported Sub2API payload."""


class Sub2APIError(RuntimeError):
    """Raised when Sub2API rejects an import request."""


def _decode_json_string(value: str) -> Any:
    try:
        return json.loads(value.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise DownloadPayloadError(f"下载内容不是有效 JSON：{exc.msg}") from exc


def _looks_like_account(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("credentials"), dict)
        and bool(value.get("platform"))
        and bool(value.get("type"))
    )


def _normalize_account(account: dict[str, Any], index: int) -> dict[str, Any]:
    normalized = dict(account)
    credentials = normalized.get("credentials")
    if not isinstance(credentials, dict) or not credentials:
        raise DownloadPayloadError(f"第 {index + 1} 个账号缺少 credentials")
    if not str(normalized.get("platform", "")).strip():
        raise DownloadPayloadError(f"第 {index + 1} 个账号缺少 platform")
    if not str(normalized.get("type", "")).strip():
        raise DownloadPayloadError(f"第 {index + 1} 个账号缺少 type")

    if not str(normalized.get("name", "")).strip():
        email = str(credentials.get("email", "")).strip()
        normalized["name"] = email or f"兑换账号-{index + 1}"

    normalized.setdefault("concurrency", DEFAULT_ACCOUNT_CONCURRENCY)
    normalized.setdefault("priority", 0)
    normalized.setdefault("extra", {})
    return normalized


def extract_sub2api_payload(
    value: bytes | str | dict[str, Any] | list[Any], *, _depth: int = 0
) -> dict[str, Any]:
    """Extract a Sub2API data payload from common download wrappers."""
    if _depth > 6:
        raise DownloadPayloadError("下载 JSON 嵌套层级过深")

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DownloadPayloadError("下载内容不是 UTF-8 JSON") from exc
    if isinstance(value, str):
        value = _decode_json_string(value)

    if isinstance(value, list):
        if not value:
            return _make_payload([], [])
        if all(_looks_like_account(item) for item in value):
            return _make_payload(
                [], [_normalize_account(item, idx) for idx, item in enumerate(value)]
            )
        return merge_sub2api_payloads(
            [extract_sub2api_payload(item, _depth=_depth + 1) for item in value]
        )

    if not isinstance(value, dict):
        raise DownloadPayloadError("下载 JSON 顶层必须是对象或数组")

    # Sub2API's standard response envelope.
    if "code" in value and "data" in value:
        if value.get("code") not in (0, "0", None):
            raise DownloadPayloadError(str(value.get("message") or "下载接口返回失败"))
        return extract_sub2api_payload(value["data"], _depth=_depth + 1)

    accounts = value.get("accounts")
    if isinstance(accounts, list):
        proxies = value.get("proxies", [])
        if not isinstance(proxies, list):
            raise DownloadPayloadError("proxies 必须是数组")
        normalized_accounts = [
            _normalize_account(item, idx)
            for idx, item in enumerate(accounts)
            if isinstance(item, dict)
        ]
        if len(normalized_accounts) != len(accounts):
            raise DownloadPayloadError("accounts 中包含非对象条目")
        return _make_payload(proxies, normalized_accounts)

    if _looks_like_account(value):
        return _make_payload([], [_normalize_account(value, 0)])

    single_account = value.get("account")
    if _looks_like_account(single_account):
        return _make_payload([], [_normalize_account(single_account, 0)])

    files = value.get("files")
    if isinstance(files, list):
        extracted: list[dict[str, Any]] = []
        for item in files:
            if isinstance(item, dict) and "content" in item:
                item = item["content"]
            extracted.append(extract_sub2api_payload(item, _depth=_depth + 1))
        return merge_sub2api_payloads(extracted)

    for key in ("data", "payload", "bundle", "result", "content"):
        nested = value.get(key)
        if isinstance(nested, (dict, list, str)):
            try:
                return extract_sub2api_payload(nested, _depth=_depth + 1)
            except DownloadPayloadError:
                continue

    raise DownloadPayloadError("未在下载 JSON 中找到 Sub2API accounts 数据")


def _make_payload(proxies: list[Any], accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "proxies": [item for item in proxies if isinstance(item, dict)],
        "accounts": accounts,
    }


def merge_sub2api_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    proxies: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    seen_proxies: set[str] = set()
    seen_accounts: set[str] = set()

    for payload in payloads:
        for proxy in payload.get("proxies", []):
            if not isinstance(proxy, dict):
                continue
            identity = str(
                proxy.get("proxy_key")
                or json.dumps(proxy, sort_keys=True, ensure_ascii=False)
            )
            if identity not in seen_proxies:
                proxies.append(proxy)
                seen_proxies.add(identity)

        for account in payload.get("accounts", []):
            if not isinstance(account, dict):
                continue
            identity = json.dumps(
                account, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            if identity not in seen_accounts:
                accounts.append(account)
                seen_accounts.add(identity)

    return _make_payload(proxies, accounts)


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _find_email(value: Any, *, depth: int = 0) -> str | None:
    if depth > 6 or not isinstance(value, dict):
        return None
    preferred_keys = ("email", "user_email", "account_email", "preferred_username")
    for key in preferred_keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and EMAIL_PATTERN.fullmatch(candidate.strip()):
            return candidate.strip()
    for nested in value.values():
        if isinstance(nested, dict):
            candidate = _find_email(nested, depth=depth + 1)
            if candidate:
                return candidate
    return None


def extract_account_email(account: dict[str, Any]) -> str | None:
    credentials = account.get("credentials")
    for source in (account, credentials):
        candidate = _find_email(source)
        if candidate:
            return candidate

    if isinstance(credentials, dict):
        for token_key in ("id_token", "access_token"):
            token = credentials.get(token_key)
            if not isinstance(token, str):
                continue
            claims = _decode_jwt_payload(token)
            candidate = _find_email(claims)
            if candidate:
                return candidate

    existing_name = str(account.get("name", "")).strip()
    return existing_name if EMAIL_PATTERN.fullmatch(existing_name) else None


def _email_in_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = EMAIL_SEARCH_PATTERN.search(value)
    return match.group(0) if match else None


def _looks_like_card_code(value: str) -> bool:
    return (
        4 <= len(value) <= 128
        and "://" not in value
        and bool(CARD_CODE_PATTERN.fullmatch(value))
    )


def extract_recovery_card_code(account: dict[str, Any]) -> str | None:
    notes = account.get("notes")
    if isinstance(notes, str):
        candidate = notes.strip()
        if _looks_like_card_code(candidate):
            return candidate

    name = str(account.get("name", "")).strip()
    if " " not in name:
        return None
    candidate = name.rsplit(" ", 1)[-1].strip()
    return candidate if _looks_like_card_code(candidate) else None


def _account_has_401_state(account: dict[str, Any]) -> bool:
    error_message = str(account.get("error_message", ""))
    temporary_reason = str(account.get("temp_unschedulable_reason", ""))
    return bool(
        HTTP_401_PATTERN.search(error_message)
        or HTTP_401_PATTERN.search(temporary_reason)
        or "oauth_401" in temporary_reason.lower()
    )


def apply_account_delivery_metadata(
    payload: dict[str, Any], card_code: str
) -> dict[str, Any]:
    normalized_code = card_code.strip()
    if not normalized_code:
        raise DownloadPayloadError("下载任务缺少兑换码，无法写入账号备注")

    accounts: list[dict[str, Any]] = []
    for index, source in enumerate(payload.get("accounts", [])):
        if not isinstance(source, dict):
            raise DownloadPayloadError(f"第 {index + 1} 个账号格式不正确")
        email = extract_account_email(source)
        if not email:
            raise DownloadPayloadError(
                f"第 {index + 1} 个账号凭据中未找到邮箱，无法设置账号名称"
            )
        account = dict(source)
        account["name"] = email
        account["notes"] = normalized_code
        accounts.append(account)

    return _make_payload(payload.get("proxies", []), accounts)


def build_api_url(base_url: str, endpoint: str) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    normalized_endpoint = "/" + endpoint.lstrip("/")
    if path.endswith("/api/v1"):
        next_path = f"{path}{normalized_endpoint}"
    else:
        next_path = f"{path}/api/v1{normalized_endpoint}"
    return urlunsplit((parsed.scheme, parsed.netloc, next_path, "", ""))


def build_import_url(base_url: str) -> str:
    return build_api_url(base_url, "/admin/accounts/batch")


def _admin_auth_headers(token: str, *, user_agent: str) -> dict[str, str]:
    credential = token.strip()
    headers = {"User-Agent": user_agent}
    if ADMIN_API_KEY_PATTERN.fullmatch(credential):
        headers["x-api-key"] = credential
    else:
        headers["Authorization"] = f"Bearer {credential}"
    return headers


def _response_data(response: httpx.Response, operation: str) -> Any:
    try:
        result = response.json()
    except ValueError as exc:
        detail = response.text[:300].strip()
        raise Sub2APIError(
            f"Sub2API {operation}返回了非 JSON 响应（HTTP {response.status_code}）：{detail}"
        ) from exc

    if response.is_error:
        message = result.get("message") if isinstance(result, dict) else None
        raise Sub2APIError(
            f"Sub2API {operation}失败（HTTP {response.status_code}）：{message or '未知错误'}"
        )
    if isinstance(result, dict) and "code" in result:
        if result.get("code") != 0:
            raise Sub2APIError(str(result.get("message") or f"Sub2API {operation}失败"))
        return result.get("data")
    return result


async def fetch_sub2api_options(
    *, base_url: str, token: str, verify_tls: bool
) -> dict[str, list[dict[str, Any]]]:
    headers = _admin_auth_headers(token, user_agent="account-import/0.4")
    try:
        async with httpx.AsyncClient(
            verify=verify_tls, follow_redirects=True, timeout=30
        ) as client:
            group_response, proxy_response = await asyncio.gather(
                client.get(
                    build_api_url(base_url, "/admin/groups/all"), headers=headers
                ),
                client.get(
                    build_api_url(base_url, "/admin/proxies/all"),
                    headers=headers,
                    params={"with_count": "true"},
                ),
            )
    except httpx.HTTPError as exc:
        raise Sub2APIError(f"连接 Sub2API 失败：{exc}") from exc

    raw_groups = _response_data(group_response, "读取分组")
    raw_proxies = _response_data(proxy_response, "读取代理")
    if not isinstance(raw_groups, list) or not isinstance(raw_proxies, list):
        raise Sub2APIError("Sub2API 分组或代理列表格式不正确")

    groups = [
        {
            "id": item.get("id"),
            "name": item.get("name", ""),
            "platform": item.get("platform", ""),
        }
        for item in raw_groups
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    ]
    proxies = [
        {
            "id": item.get("id"),
            "name": item.get("name", ""),
            "protocol": item.get("protocol", ""),
            "host": item.get("host", ""),
            "port": item.get("port", 0),
            "account_count": item.get("account_count", 0) or 0,
        }
        for item in raw_proxies
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    ]
    return {"groups": groups, "proxies": proxies}


async def fetch_sub2api_401_accounts(
    *, base_url: str, token: str, verify_tls: bool, group_id: int
) -> dict[str, Any]:
    headers = _admin_auth_headers(token, user_agent="account-import/0.4")
    page = 1
    page_size = 1000
    scanned = 0
    other_errors = 0
    candidates: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            verify=verify_tls, follow_redirects=True, timeout=60
        ) as client:
            while True:
                response = await client.get(
                    build_api_url(base_url, "/admin/accounts"),
                    headers=headers,
                    params={
                        "page": str(page),
                        "page_size": str(page_size),
                        "lite": "true",
                        "group": str(group_id),
                    },
                )
                data = _response_data(response, "读取账号")
                if not isinstance(data, dict) or not isinstance(
                    data.get("items"), list
                ):
                    raise Sub2APIError("Sub2API 账号列表格式不正确")
                items = data["items"]
                scanned += len(items)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if not _account_has_401_state(item):
                        if item.get("status") == "error":
                            other_errors += 1
                        continue
                    account_id = item.get("id")
                    if not isinstance(account_id, int):
                        continue
                    card_code = extract_recovery_card_code(item)
                    candidates.append(
                        {
                            "id": account_id,
                            "name": str(item.get("name", "")),
                            "email": _email_in_text(item.get("name")),
                            "platform": str(item.get("platform", "")),
                            "type": str(item.get("type", "")),
                            "status": str(item.get("status", "")),
                            "error_message": str(item.get("error_message", ""))[:300],
                            "card_code": card_code,
                        }
                    )

                pages = int(data.get("pages", 1) or 1)
                if page >= pages or not items:
                    break
                page += 1
    except httpx.HTTPError as exc:
        raise Sub2APIError(f"连接 Sub2API 失败：{exc}") from exc

    recoverable = [item for item in candidates if item["card_code"]]
    return {
        "group_id": group_id,
        "scanned": scanned,
        "detected_401": len(candidates),
        "recoverable": len(recoverable),
        "missing_card_code": len(candidates) - len(recoverable),
        "other_errors": other_errors,
        "unique_codes": len({item["card_code"] for item in recoverable}),
        "accounts": candidates,
    }


async def replace_sub2api_account_credentials(
    *,
    base_url: str,
    token: str,
    verify_tls: bool,
    account_id: int,
    account: dict[str, Any],
    card_code: str,
) -> dict[str, Any]:
    email = extract_account_email(account)
    credentials = account.get("credentials")
    if not email or not isinstance(credentials, dict) or not credentials:
        raise Sub2APIError("找回结果缺少邮箱或凭据")

    headers = _admin_auth_headers(token, user_agent="account-import/0.4")
    update_body = {
        "name": email,
        "notes": card_code,
        "type": account.get("type"),
        "credentials": credentials,
        "status": "active",
    }
    try:
        async with httpx.AsyncClient(
            verify=verify_tls, follow_redirects=True, timeout=120
        ) as client:
            update_response = await client.put(
                build_api_url(base_url, f"/admin/accounts/{account_id}"),
                headers=headers,
                json=update_body,
            )
            _response_data(update_response, f"更新账号 {account_id}")
            clear_response = await client.post(
                build_api_url(base_url, f"/admin/accounts/{account_id}/clear-error"),
                headers=headers,
            )
            result = _response_data(
                clear_response, f"清除账号 {account_id} 的 401 状态"
            )
    except httpx.HTTPError as exc:
        raise Sub2APIError(f"连接 Sub2API 失败：{exc}") from exc

    return result if isinstance(result, dict) else {"id": account_id, "name": email}


def _account_create_payload(
    account: dict[str, Any], group_id: int | None, proxy_id: int | None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": account["name"],
        "notes": account.get("notes"),
        "platform": account["platform"],
        "type": account["type"],
        "credentials": account["credentials"],
        "extra": account.get("extra") or {},
        "proxy_id": proxy_id,
        "concurrency": account.get("concurrency", DEFAULT_ACCOUNT_CONCURRENCY),
        "priority": account.get("priority", 0),
        "group_ids": [group_id] if group_id is not None else [],
    }
    for key in (
        "rate_multiplier",
        "load_factor",
        "expires_at",
        "auto_pause_on_expired",
        "upstream_billing_probe_enabled",
    ):
        if key in account:
            body[key] = account[key]
    return body


async def import_to_sub2api(
    *,
    base_url: str,
    token: str,
    payload: dict[str, Any],
    verify_tls: bool,
    idempotency_key: str,
    group_id: int | None,
    proxy_id: int | None,
) -> dict[str, Any]:
    url = build_import_url(base_url)
    accounts = [
        _account_create_payload(account, group_id, proxy_id)
        for account in payload.get("accounts", [])
        if isinstance(account, dict)
    ]
    if not accounts:
        raise Sub2APIError("没有可创建的 Sub2API 账号")

    created = 0
    failed = 0
    errors: list[dict[str, str]] = []
    account_results: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(
            verify=verify_tls, follow_redirects=True, timeout=120
        ) as client:
            for batch_index, start in enumerate(range(0, len(accounts), 100)):
                batch = accounts[start : start + 100]
                headers = _admin_auth_headers(token, user_agent="account-import/0.4")
                headers["Idempotency-Key"] = f"{idempotency_key}-{batch_index + 1}"
                response = await client.post(
                    url, headers=headers, json={"accounts": batch}
                )
                result = _response_data(response, "批量创建账号")
                if not isinstance(result, dict):
                    raise Sub2APIError("Sub2API 批量创建结果格式不正确")
                created += int(result.get("success", 0) or 0)
                failed += int(result.get("failed", 0) or 0)
                for item in result.get("results", []) or []:
                    if not isinstance(item, dict):
                        continue
                    safe_result = {
                        "id": item.get("id")
                        if isinstance(item.get("id"), int)
                        else None,
                        "name": str(item.get("name", "")),
                        "success": bool(item.get("success", False)),
                        "error": str(item.get("error", "")),
                    }
                    account_results.append(safe_result)
                    if not safe_result["success"]:
                        errors.append(
                            {
                                "kind": "account",
                                "name": safe_result["name"],
                                "message": safe_result["error"] or "创建失败",
                            }
                        )
    except httpx.HTTPError as exc:
        raise Sub2APIError(f"连接 Sub2API 失败：{exc}") from exc

    return {
        "proxy_created": 0,
        "proxy_reused": 0,
        "proxy_failed": 0,
        "account_created": created,
        "account_failed": failed,
        "errors": errors,
        "results": account_results,
    }
