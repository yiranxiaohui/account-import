import json

import httpx
import pytest

from app.sub2api import (
    DownloadPayloadError,
    apply_account_delivery_metadata,
    build_import_url,
    extract_sub2api_payload,
    fetch_sub2api_401_accounts,
    fetch_sub2api_options,
    import_to_sub2api,
    merge_sub2api_payloads,
    replace_sub2api_account_credentials,
)

ACCOUNT = {
    "name": "OpenAI account",
    "platform": "openai",
    "type": "oauth",
    "credentials": {"access_token": "access", "refresh_token": "refresh"},
    "concurrency": 2,
    "priority": 1,
}
ADMIN_API_KEY = "admin-" + "a" * 64


def test_extracts_standard_sub2api_payload_from_bytes():
    source = json.dumps(
        {"type": "sub2api-data", "version": 1, "proxies": [], "accounts": [ACCOUNT]}
    ).encode()

    payload = extract_sub2api_payload(source)

    assert payload["type"] == "sub2api-data"
    assert payload["accounts"] == [ACCOUNT | {"extra": {}}]


def test_extracts_response_envelope_and_single_account():
    payload = extract_sub2api_payload({"code": 0, "data": {"account": ACCOUNT}})

    assert len(payload["accounts"]) == 1
    assert payload["accounts"][0]["credentials"]["access_token"] == "access"


def test_merges_files_and_removes_exact_duplicates():
    first = extract_sub2api_payload([ACCOUNT])
    second = extract_sub2api_payload({"accounts": [ACCOUNT], "proxies": []})

    merged = merge_sub2api_payloads([first, second])

    assert len(merged["accounts"]) == 1


def test_delivery_metadata_uses_email_for_name_and_card_code_for_notes():
    account = ACCOUNT | {
        "credentials": ACCOUNT["credentials"] | {"email": "owner@example.com"}
    }

    payload = apply_account_delivery_metadata(
        extract_sub2api_payload([account]), "RCL-AAAA-BBBB"
    )

    assert payload["accounts"][0]["name"] == "owner@example.com"
    assert payload["accounts"][0]["notes"] == "RCL-AAAA-BBBB"


def test_delivery_metadata_rejects_account_without_email():
    with pytest.raises(DownloadPayloadError, match="邮箱"):
        apply_account_delivery_metadata(
            extract_sub2api_payload([ACCOUNT]), "RCL-AAAA-BBBB"
        )


def test_invalid_download_is_rejected():
    with pytest.raises(DownloadPayloadError, match="accounts"):
        extract_sub2api_payload({"unexpected": True})


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://localhost:8080", "http://localhost:8080/api/v1/admin/accounts/batch"),
        (
            "https://sub.example.com/api/v1",
            "https://sub.example.com/api/v1/admin/accounts/batch",
        ),
    ],
)
def test_build_import_url(base: str, expected: str):
    assert build_import_url(base) == expected


@pytest.mark.parametrize("token", ["admin-token", ADMIN_API_KEY])
@pytest.mark.asyncio
async def test_fetches_group_and_proxy_options_without_exposing_proxy_secrets(
    monkeypatch, token: str
):
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if token == ADMIN_API_KEY:
            assert request.headers["x-api-key"] == ADMIN_API_KEY
            assert "Authorization" not in request.headers
        else:
            assert request.headers["Authorization"] == "Bearer admin-token"
            assert "x-api-key" not in request.headers
        if request.url.path.endswith("/admin/groups/all"):
            data = [{"id": 7, "name": "OpenAI 主组", "platform": "openai"}]
        else:
            assert request.url.path.endswith("/admin/proxies/all")
            data = [
                {
                    "id": 9,
                    "name": "HK",
                    "protocol": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "account_count": 3,
                    "password": "must-not-leak",
                }
            ]
        return httpx.Response(200, json={"code": 0, "message": "success", "data": data})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.sub2api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await fetch_sub2api_options(
        base_url="https://sub2api.example.com",
        token=token,
        verify_tls=True,
    )

    assert result["groups"] == [{"id": 7, "name": "OpenAI 主组", "platform": "openai"}]
    assert result["proxies"][0]["id"] == 9
    assert "password" not in result["proxies"][0]


@pytest.mark.asyncio
async def test_batch_create_sends_group_proxy_email_name_and_card_note(monkeypatch):
    real_client = httpx.AsyncClient
    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/v1/admin/accounts/batch")
        assert request.headers["x-api-key"] == ADMIN_API_KEY
        assert "Authorization" not in request.headers
        received.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "success",
                "data": {
                    "success": 1,
                    "failed": 0,
                    "results": [
                        {"name": "owner@example.com", "id": 101, "success": True}
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.sub2api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    payload = apply_account_delivery_metadata(
        extract_sub2api_payload(
            [
                ACCOUNT
                | {
                    "credentials": ACCOUNT["credentials"]
                    | {"email": "owner@example.com"}
                }
            ]
        ),
        "RCL-AAAA-BBBB",
    )

    result = await import_to_sub2api(
        base_url="https://sub2api.example.com",
        token=ADMIN_API_KEY,
        payload=payload,
        verify_tls=True,
        idempotency_key="job-1",
        group_id=7,
        proxy_id=9,
    )

    account = received["accounts"][0]
    assert account["name"] == "owner@example.com"
    assert account["notes"] == "RCL-AAAA-BBBB"
    assert account["group_ids"] == [7]
    assert account["proxy_id"] == 9
    assert result["account_created"] == 1
    assert result["results"] == [
        {
            "id": 101,
            "name": "owner@example.com",
            "success": True,
            "error": "",
        }
    ]


@pytest.mark.asyncio
async def test_scans_recorded_401_accounts_and_reads_new_and_legacy_codes(monkeypatch):
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/v1/admin/accounts")
        assert request.url.params["page_size"] == "1000"
        data = {
            "items": [
                {
                    "id": 1,
                    "name": "owner@example.com",
                    "notes": "RCL-AAAA-BBBB",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "error",
                    "error_message": "Authentication failed (401)",
                },
                {
                    "id": 2,
                    "name": "Legacy owner legacy@example.com RCL-CCCC-DDDD",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "active",
                    "error_message": "",
                    "temp_unschedulable_reason": "oauth_401",
                },
                {
                    "id": 3,
                    "name": "healthy@example.com",
                    "notes": "RCL-EEEE-FFFF",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "active",
                    "error_message": "",
                },
                {
                    "id": 4,
                    "name": "no-code@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "error",
                    "error_message": "upstream returned 401",
                },
            ],
            "total": 4,
            "page": 1,
            "page_size": 1000,
            "pages": 1,
        }
        return httpx.Response(200, json={"code": 0, "data": data})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.sub2api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await fetch_sub2api_401_accounts(
        base_url="https://sub2api.example.com",
        token="admin-token",
        verify_tls=True,
    )

    assert result["scanned"] == 4
    assert result["detected_401"] == 3
    assert result["recoverable"] == 2
    assert result["missing_card_code"] == 1
    assert result["unique_codes"] == 2
    assert result["accounts"][0]["card_code"] == "RCL-AAAA-BBBB"
    assert result["accounts"][1]["card_code"] == "RCL-CCCC-DDDD"
    assert result["accounts"][1]["email"] == "legacy@example.com"


@pytest.mark.asyncio
async def test_replaces_credentials_in_place_and_clears_401_state(monkeypatch):
    real_client = httpx.AsyncClient
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"id": 41, "name": "owner@example.com", "status": "active"},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.sub2api.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    result = await replace_sub2api_account_credentials(
        base_url="https://sub2api.example.com",
        token="admin-token",
        verify_tls=True,
        account_id=41,
        account=ACCOUNT
        | {"credentials": ACCOUNT["credentials"] | {"email": "owner@example.com"}},
        card_code="RCL-AAAA-BBBB",
    )

    assert requests[0][0:2] == ("PUT", "/api/v1/admin/accounts/41")
    assert requests[0][2]["name"] == "owner@example.com"
    assert requests[0][2]["notes"] == "RCL-AAAA-BBBB"
    assert requests[0][2]["status"] == "active"
    assert requests[1][0:2] == (
        "POST",
        "/api/v1/admin/accounts/41/clear-error",
    )
    assert result["status"] == "active"
