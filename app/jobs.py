from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redeem_api_sdk import BatchReclaimResult, ReclaimTask, RedeemClient

from .ledger import AccountLedger, AccountLedgerError
from .models import ImportJobRequest, JobSnapshot, Recover401JobRequest
from .sub2api import (
    DownloadPayloadError,
    apply_account_delivery_metadata,
    extract_account_email,
    extract_sub2api_payload,
    fetch_sub2api_401_accounts,
    import_to_sub2api,
    merge_sub2api_payloads,
    replace_sub2api_account_credentials,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


@dataclass
class JobRecord:
    id: str
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    message: str = "任务已创建，等待开始"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events: list[dict[str, str]] = field(default_factory=list)
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def update(
        self,
        *,
        stage: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        status: str | None = None,
    ) -> None:
        if stage is not None:
            self.stage = stage
        if progress is not None:
            self.progress = max(0, min(100, progress))
        if message is not None:
            self.message = message
        if status is not None:
            self.status = status
        self.updated_at = utc_now()

    def log(self, message: str, level: str = "info") -> None:
        self.events.append({"time": utc_now(), "level": level, "message": message})
        self.events = self.events[-120:]
        self.updated_at = utc_now()

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot.model_validate(
            {
                "id": self.id,
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "message": self.message,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "summary": self.summary,
                "error": self.error,
                "events": self.events,
            }
        )


class JobManager:
    def __init__(self, ledger: AccountLedger | None = None) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.ledger = ledger
        self.poll_interval = max(
            1.0,
            float(
                os.getenv("ACCOUNT_IMPORT_POLL_INTERVAL")
                or os.getenv("TEAM_IMPORT_POLL_INTERVAL", "5")
            ),
        )
        self.max_wait = max(
            30.0,
            float(
                os.getenv("ACCOUNT_IMPORT_MAX_WAIT")
                or os.getenv("TEAM_IMPORT_MAX_WAIT", "600")
            ),
        )
        self.empty_result_retry_delay = max(
            0.0,
            float(
                os.getenv("ACCOUNT_IMPORT_EMPTY_RESULT_RETRY_DELAY")
                or os.getenv("TEAM_IMPORT_EMPTY_RESULT_RETRY_DELAY", "3")
            ),
        )

    def create(self, request: ImportJobRequest) -> JobRecord:
        self._prune()
        record = JobRecord(id=uuid.uuid4().hex)
        record.log(f"已接收 {len(request.card_codes)} 个兑换码")
        self.jobs[record.id] = record
        record.task = asyncio.create_task(self._run(record, request))
        return record

    def create_recovery(self, request: Recover401JobRequest) -> JobRecord:
        self._prune()
        record = JobRecord(
            id=uuid.uuid4().hex,
            summary={"operation": "recover_401"},
            message="401 找回任务已创建，等待开始",
        )
        record.log(f"已选择 {len(request.account_ids)} 个 Sub2API 账号")
        self.jobs[record.id] = record
        record.task = asyncio.create_task(self._run_recovery(record, request))
        return record

    def get(self, job_id: str) -> JobRecord | None:
        return self.jobs.get(job_id)

    def _prune(self) -> None:
        if len(self.jobs) < 200:
            return
        terminal = [
            item
            for item in self.jobs.values()
            if item.status in {"succeeded", "partial", "failed"}
        ]
        terminal.sort(key=lambda item: item.updated_at)
        for item in terminal[: max(0, len(self.jobs) - 150)]:
            self.jobs.pop(item.id, None)

    async def _run(self, job: JobRecord, request: ImportJobRequest) -> None:
        try:
            await self._execute(job, request)
        except Exception as exc:  # noqa: BLE001 - background jobs must always reach a terminal state
            job.error = str(exc)
            job.log(str(exc), "error")
            job.update(status="failed", stage="failed", message="任务执行失败")

    async def _run_recovery(
        self, job: JobRecord, request: Recover401JobRequest
    ) -> None:
        try:
            await self._execute_recovery(job, request)
        except Exception as exc:  # noqa: BLE001 - background jobs must always reach a terminal state
            job.error = str(exc)
            job.log(str(exc), "error")
            job.update(status="failed", stage="failed", message="401 找回任务执行失败")

    async def _execute(self, job: JobRecord, request: ImportJobRequest) -> None:
        codes = request.card_codes
        client = RedeemClient(str(request.redeem_base_url).rstrip("/"), timeout=45)
        job.update(
            status="running",
            stage="checking",
            progress=4,
            message="正在验证兑换码并提交任务",
        )

        batches = chunked(codes, 20)
        all_downloadables: list[ReclaimTask] = []
        final_results: list[BatchReclaimResult] = []
        for index, batch in enumerate(batches):
            batch_no = index + 1
            job.update(
                stage="redeeming",
                progress=10 + int(48 * index / len(batches)),
                message=f"正在处理第 {batch_no}/{len(batches)} 批兑换码",
            )
            job.log(f"第 {batch_no} 批开始，共 {len(batch)} 个兑换码")
            initial = await asyncio.to_thread(client.batch_reclaim, batch, request.mode)
            if not initial.ok:
                raise RuntimeError(
                    f"第 {batch_no} 批提交失败：{initial.error or '未知错误'}"
                )
            if self._is_empty_valid_batch(initial):
                job.log(
                    f"第 {batch_no} 批兑换码有效但暂未返回关联账号，"
                    f"{self.empty_result_retry_delay:g} 秒后自动重试",
                    "warning",
                )
                await asyncio.sleep(self.empty_result_retry_delay)
                retried = await asyncio.to_thread(
                    client.batch_reclaim, batch, request.mode
                )
                if retried.ok:
                    initial = retried
                else:
                    job.log(
                        f"第 {batch_no} 批自动重试失败：{retried.error or '未知错误'}",
                        "warning",
                    )

            final = await self._wait_for_batch(
                job, client, batch, initial, index, len(batches)
            )
            final_results.append(final)
            all_downloadables.extend(
                task
                for task in final.all_tasks
                if task.status == "done" and task.order_no and task.download_token
            )
            batch_issues, batch_issue_count = self._redeem_issues([final], batch)
            if batch_issues:
                job.log(
                    f"第 {batch_no} 批存在未生成下载项："
                    f"{self._format_redeem_issues(batch_issues)}",
                    "warning",
                )
            job.log(
                f"第 {batch_no} 批处理完成：完成 {final.done}，失败 {final.failed}，不可找回 {final.unreclaimable}",
                "success"
                if final.failed == 0 and batch_issue_count == 0
                else "warning",
            )

        unique_downloads: list[ReclaimTask] = []
        seen_downloads: set[tuple[str, str]] = set()
        for task in all_downloadables:
            identity = (task.order_no, task.download_token)
            if identity not in seen_downloads:
                unique_downloads.append(task)
                seen_downloads.add(identity)

        redeem_issues, redeem_issue_count = self._redeem_issues(final_results, codes)
        job.summary["redeem"] = {
            "batches": len(batches),
            "requested_cards": sum(item.requested_cards for item in final_results),
            "valid_cards": sum(item.valid_cards for item in final_results),
            "distinct_resources": sum(
                item.distinct_resources for item in final_results
            ),
            "scanned_resources": sum(item.scanned_resources for item in final_results),
            "skipped_not_401": sum(item.skipped_not_401 for item in final_results),
            "done": sum(item.done for item in final_results),
            "failed": sum(item.failed for item in final_results),
            "unreclaimable": sum(item.unreclaimable for item in final_results),
            "downloadable": len(unique_downloads),
            "issue_count": redeem_issue_count,
            "issues": [
                {"message": message, "count": count}
                for message, count in redeem_issues.most_common()
            ],
        }
        if not unique_downloads:
            mode_hint = (
                "；使用“下载全部额度”模式可同时处理当前正常的账号"
                if request.mode == "401"
                else ""
            )
            issue_hint = (
                f"：{self._format_redeem_issues(redeem_issues)}"
                if redeem_issues
                else ""
            )
            raise RuntimeError(f"没有取得可下载的额度文件{issue_hint}{mode_hint}")

        payloads: list[dict[str, Any]] = []
        download_errors: list[dict[str, str]] = []
        total_downloads = len(unique_downloads)
        for index, task in enumerate(unique_downloads):
            job.update(
                stage="downloading",
                progress=60 + int(20 * index / total_downloads),
                message=f"正在下载额度文件 {index + 1}/{total_downloads}",
            )
            raw = await asyncio.to_thread(
                client.download, task.order_no, task.download_token
            )
            if raw is None:
                download_errors.append(
                    {"order_no": task.order_no, "message": "下载请求失败"}
                )
                job.log(f"订单 {task.order_no} 下载失败", "warning")
                continue
            try:
                payload = extract_sub2api_payload(raw)
                payload = apply_account_delivery_metadata(payload, task.card_code)
            except DownloadPayloadError as exc:
                download_errors.append({"order_no": task.order_no, "message": str(exc)})
                job.log(f"订单 {task.order_no} 无法解析：{exc}", "warning")
                continue
            payloads.append(payload)
            job.log(
                f"订单 {task.order_no} 下载成功，发现 {len(payload['accounts'])} 个账号",
                "success",
            )

        merged = merge_sub2api_payloads(payloads)
        account_count = len(merged["accounts"])
        job.summary["download"] = {
            "files": len(payloads),
            "failed": len(download_errors),
            "accounts": account_count,
            "errors": download_errors,
        }
        if account_count == 0:
            raise RuntimeError("额度文件均未包含可导入的 Sub2API 账号")

        job.update(
            stage="importing",
            progress=84,
            message=f"正在向 Sub2API 导入 {account_count} 个账号",
        )
        target_parts = [
            f"分组 {request.group_id}" if request.group_id else "不绑定分组"
        ]
        target_parts.append(f"代理 {request.proxy_id}" if request.proxy_id else "直连")
        job.log(
            f"开始向 Sub2API 导入 {account_count} 个账号（{'，'.join(target_parts)}）"
        )
        import_result = await import_to_sub2api(
            base_url=str(request.sub2api_base_url).rstrip("/"),
            token=request.sub2api_token.get_secret_value(),
            payload=merged,
            verify_tls=request.verify_sub2api_tls,
            idempotency_key=f"account-import-{job.id}",
            group_id=request.group_id,
            proxy_id=request.proxy_id,
        )
        job.summary["import"] = import_result
        await self._record_import_results(job, request, merged, import_result)

        import_failed = int(import_result.get("account_failed", 0) or 0) + int(
            import_result.get("proxy_failed", 0) or 0
        )
        has_warnings = bool(download_errors or import_failed)
        status = "partial" if has_warnings else "succeeded"
        created = int(import_result.get("account_created", 0) or 0)
        job.log(
            f"Sub2API 导入完成：成功 {created}，失败 {import_failed}",
            "warning" if has_warnings else "success",
        )
        job.update(
            status=status,
            stage="completed",
            progress=100,
            message=f"导入完成，成功创建 {created} 个账号"
            + ("，部分项目需要检查" if has_warnings else ""),
        )

    async def _execute_recovery(
        self, job: JobRecord, request: Recover401JobRequest
    ) -> None:
        sub2api_kwargs = {
            "base_url": str(request.sub2api_base_url).rstrip("/"),
            "token": request.sub2api_token.get_secret_value(),
            "verify_tls": request.verify_sub2api_tls,
        }
        job.update(
            status="running",
            stage="checking",
            progress=4,
            message="正在读取 Sub2API 的 401 账号",
        )
        scan = await fetch_sub2api_401_accounts(**sub2api_kwargs)
        selected_ids = set(request.account_ids)
        targets = [
            account
            for account in scan["accounts"]
            if account["id"] in selected_ids and account.get("card_code")
        ]
        codes = list(dict.fromkeys(str(account["card_code"]) for account in targets))
        job.summary["scan"] = {
            "scanned": scan["scanned"],
            "detected_401": scan["detected_401"],
            "selected": len(targets),
            "unique_codes": len(codes),
        }
        if not targets:
            job.log("所选账号当前已无可找回的 401 状态", "success")
            job.summary["recovery"] = {"updated": 0, "failed": 0, "errors": []}
            job.update(
                status="succeeded",
                stage="completed",
                progress=100,
                message="所选账号当前无需找回",
            )
            return

        job.log(f"发现 {len(targets)} 个可找回账号，涉及 {len(codes)} 个兑换码")
        client = RedeemClient(str(request.redeem_base_url).rstrip("/"), timeout=45)
        batches = chunked(codes, 20)
        all_downloadables: list[ReclaimTask] = []
        final_results: list[BatchReclaimResult] = []
        for index, batch in enumerate(batches):
            batch_no = index + 1
            job.update(
                stage="redeeming",
                progress=10 + int(45 * index / len(batches)),
                message=f"正在找回第 {batch_no}/{len(batches)} 批 401 凭据",
            )
            initial = await asyncio.to_thread(client.batch_reclaim, batch, "401")
            if not initial.ok:
                raise RuntimeError(
                    f"第 {batch_no} 批 401 找回提交失败：{initial.error or '未知错误'}"
                )
            final = await self._wait_for_batch(
                job, client, batch, initial, index, len(batches)
            )
            final_results.append(final)
            all_downloadables.extend(
                task
                for task in final.all_tasks
                if task.status == "done" and task.order_no and task.download_token
            )

        unique_downloads: list[ReclaimTask] = []
        seen_downloads: set[tuple[str, str]] = set()
        for task in all_downloadables:
            identity = (task.order_no, task.download_token)
            if identity not in seen_downloads:
                unique_downloads.append(task)
                seen_downloads.add(identity)

        job.summary["redeem"] = {
            "batches": len(batches),
            "done": sum(item.done for item in final_results),
            "failed": sum(item.failed for item in final_results),
            "downloadable": len(unique_downloads),
        }
        if not unique_downloads:
            job.log("兑换服务未返回可下载的新凭据", "warning")
            job.summary["recovery"] = {
                "updated": 0,
                "failed": len(targets),
                "errors": [{"message": "没有可下载的新凭据"}],
            }
            job.update(
                status="partial",
                stage="completed",
                progress=100,
                message="未取得可用于更新的凭据",
            )
            return

        recovered_by_code: dict[str, list[dict[str, Any]]] = {}
        errors: list[dict[str, Any]] = []
        for index, task in enumerate(unique_downloads):
            job.update(
                stage="downloading",
                progress=60 + int(18 * index / len(unique_downloads)),
                message=f"正在下载找回凭据 {index + 1}/{len(unique_downloads)}",
            )
            raw = await asyncio.to_thread(
                client.download, task.order_no, task.download_token
            )
            if raw is None:
                errors.append(
                    {
                        "card_code": task.card_code,
                        "message": f"订单 {task.order_no} 下载失败",
                    }
                )
                continue
            try:
                payload = apply_account_delivery_metadata(
                    extract_sub2api_payload(raw), task.card_code
                )
            except DownloadPayloadError as exc:
                errors.append({"card_code": task.card_code, "message": str(exc)})
                continue
            recovered_by_code.setdefault(task.card_code, []).extend(payload["accounts"])

        matches, match_errors = self._match_recovered_accounts(
            targets, recovered_by_code
        )
        errors.extend(match_errors)
        updated = 0
        updated_ids: list[int] = []
        for index, (target, account) in enumerate(matches):
            job.update(
                stage="importing",
                progress=82 + int(16 * index / max(len(matches), 1)),
                message=f"正在更新 Sub2API 账号 {index + 1}/{len(matches)}",
            )
            try:
                await replace_sub2api_account_credentials(
                    **sub2api_kwargs,
                    account_id=int(target["id"]),
                    account=account,
                    card_code=str(target["card_code"]),
                )
            except RuntimeError as exc:
                errors.append(
                    {
                        "account_id": target["id"],
                        "name": target["name"],
                        "message": str(exc),
                    }
                )
                job.log(f"账号 {target['name']} 更新失败：{exc}", "warning")
                await self._record_ledger_event(
                    job,
                    operation="recover_401",
                    status="failed",
                    sub2api_base_url=str(request.sub2api_base_url),
                    sub2api_account_id=int(target["id"]),
                    email=str(target.get("email") or target["name"]),
                    card_code=str(target["card_code"]),
                    platform=str(target.get("platform", "")),
                    message=str(exc),
                )
                continue
            updated += 1
            updated_ids.append(int(target["id"]))
            await self._record_ledger_event(
                job,
                operation="recover_401",
                status="success",
                sub2api_base_url=str(request.sub2api_base_url),
                sub2api_account_id=int(target["id"]),
                email=str(extract_account_email(account) or target["name"]),
                card_code=str(target["card_code"]),
                platform=str(target.get("platform", "")),
                message="401 凭据已更新并清除错误状态",
            )
            job.log(f"账号 {target['name']} 的凭据已更新并清除 401", "success")

        failed = len(targets) - updated
        job.summary["recovery"] = {
            "selected": len(targets),
            "downloaded_accounts": sum(
                len(accounts) for accounts in recovered_by_code.values()
            ),
            "updated": updated,
            "updated_ids": updated_ids,
            "failed": failed,
            "errors": errors,
        }
        status = "succeeded" if failed == 0 else "partial"
        job.update(
            status=status,
            stage="completed",
            progress=100,
            message=f"401 找回完成，成功更新 {updated} 个账号"
            + (f"，{failed} 个需检查" if failed else ""),
        )

    @staticmethod
    def _match_recovered_accounts(
        targets: list[dict[str, Any]],
        recovered_by_code: dict[str, list[dict[str, Any]]],
    ) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        errors: list[dict[str, Any]] = []
        targets_by_code: dict[str, list[dict[str, Any]]] = {}
        for target in targets:
            targets_by_code.setdefault(str(target["card_code"]), []).append(target)

        for card_code, code_targets in targets_by_code.items():
            recovered = recovered_by_code.get(card_code, [])
            available = set(range(len(recovered)))
            pending: list[dict[str, Any]] = []
            for target in code_targets:
                target_email = str(target.get("email") or "").casefold()
                exact = [
                    index
                    for index in available
                    if target_email
                    and str(extract_account_email(recovered[index]) or "").casefold()
                    == target_email
                    and recovered[index].get("platform") == target.get("platform")
                ]
                if len(exact) == 1:
                    index = exact[0]
                    available.remove(index)
                    matches.append((target, recovered[index]))
                else:
                    pending.append(target)

            if len(pending) == 1 and len(available) == 1:
                target = pending.pop()
                index = available.pop()
                if recovered[index].get("platform") == target.get("platform"):
                    matches.append((target, recovered[index]))
                else:
                    pending.append(target)

            for target in pending:
                errors.append(
                    {
                        "account_id": target["id"],
                        "name": target["name"],
                        "card_code": card_code,
                        "message": "找回结果中没有唯一匹配的同平台邮箱账号",
                    }
                )

        return matches, errors

    async def _record_import_results(
        self,
        job: JobRecord,
        request: ImportJobRequest,
        payload: dict[str, Any],
        import_result: dict[str, Any],
    ) -> None:
        if self.ledger is None:
            return
        accounts_by_name: dict[str, list[dict[str, Any]]] = {}
        for account in payload.get("accounts", []):
            if not isinstance(account, dict):
                continue
            email = extract_account_email(account)
            if email:
                accounts_by_name.setdefault(email.casefold(), []).append(account)

        for result in import_result.get("results", []) or []:
            if not isinstance(result, dict):
                continue
            name = str(result.get("name", "")).strip()
            sources = accounts_by_name.get(name.casefold(), [])
            source = sources.pop(0) if sources else None
            if source is None:
                job.log(f"本地账本无法匹配 Sub2API 返回账号：{name}", "warning")
                continue
            success = bool(result.get("success", False))
            account_id = result.get("id")
            await self._record_ledger_event(
                job,
                operation="import",
                status="success" if success else "failed",
                sub2api_base_url=str(request.sub2api_base_url),
                sub2api_account_id=(
                    account_id if isinstance(account_id, int) else None
                ),
                email=str(extract_account_email(source) or name),
                card_code=str(source.get("notes", "")),
                platform=str(source.get("platform", "")),
                message=(
                    "账号已创建"
                    if success
                    else str(result.get("error") or "Sub2API 创建账号失败")
                ),
            )

    async def _record_ledger_event(
        self,
        job: JobRecord,
        *,
        operation: str,
        status: str,
        sub2api_base_url: str,
        sub2api_account_id: int | None,
        email: str,
        card_code: str,
        platform: str,
        message: str,
    ) -> None:
        if self.ledger is None:
            return
        try:
            await asyncio.to_thread(
                self.ledger.record,
                operation=operation,
                status=status,
                job_id=job.id,
                sub2api_base_url=sub2api_base_url,
                sub2api_account_id=sub2api_account_id,
                email=email,
                card_code=card_code,
                platform=platform,
                message=message,
            )
        except AccountLedgerError as exc:
            job.summary["ledger_error"] = str(exc)
            job.log(str(exc), "warning")

    async def _wait_for_batch(
        self,
        job: JobRecord,
        client: RedeemClient,
        codes: list[str],
        initial: BatchReclaimResult,
        batch_index: int,
        batch_count: int,
    ) -> BatchReclaimResult:
        result = initial
        deadline = time.monotonic() + self.max_wait
        transient_failures = 0
        while result.queued > 0 or result.already_running > 0:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"第 {batch_index + 1} 批等待超时，请稍后重试")
            batch_progress = self._batch_progress(result)
            overall = 10 + int(48 * ((batch_index + batch_progress) / batch_count))
            job.update(
                progress=overall,
                message=(
                    f"第 {batch_index + 1}/{batch_count} 批处理中："
                    f"排队 {result.queued}，运行 {result.already_running}，完成 {result.done}"
                ),
            )
            await asyncio.sleep(self.poll_interval)
            refreshed = await asyncio.to_thread(client.refresh_progress, codes)
            if not refreshed.ok:
                transient_failures += 1
                if transient_failures >= 5:
                    raise RuntimeError(
                        f"连续刷新兑换进度失败：{refreshed.error or '未知错误'}"
                    )
                continue
            transient_failures = 0
            result = refreshed

        # Refresh once more when the initial response is already terminal, so tokens that
        # appeared just after submission are included in the final snapshot.
        if result is initial:
            refreshed = await asyncio.to_thread(client.refresh_progress, codes)
            if refreshed.ok:
                result = refreshed
        return result

    @staticmethod
    def _is_empty_valid_batch(result: BatchReclaimResult) -> bool:
        return (
            result.ok
            and result.valid_cards > 0
            and result.distinct_resources == 0
            and not result.all_tasks
            and result.queued == 0
            and result.already_running == 0
            and result.done == 0
            and result.failed == 0
            and result.unreclaimable == 0
            and result.not_owned == 0
            and result.skipped == 0
            and result.skipped_not_401 == 0
        )

    @classmethod
    def _redeem_issues(
        cls,
        results: list[BatchReclaimResult],
        card_codes: list[str],
    ) -> tuple[Counter[str], int]:
        issues: Counter[str] = Counter()
        issue_count = 0
        for result in results:
            card_errors = 0
            for card in result.cards:
                if not isinstance(card, dict):
                    continue
                message = str(card.get("error") or "").strip()
                if not message:
                    continue
                card_errors += 1
                issue_count += 1
                issues[cls._sanitize_redeem_message(message, card_codes)] += 1

            invalid_without_detail = max(
                result.requested_cards - result.valid_cards - card_errors,
                0,
            )
            if invalid_without_detail:
                issue_count += invalid_without_detail
                issues["兑换服务未识别为有效兑换码"] += invalid_without_detail

            if cls._is_empty_valid_batch(result) and card_errors == 0:
                issue_count += result.valid_cards
                issues["兑换码有效，但兑换服务未返回关联账号"] += result.valid_cards

            if result.skipped_not_401 and not result.all_tasks:
                issue_count += result.skipped_not_401
                issues["账号当前不是 401，无需找回"] += result.skipped_not_401

            for task in result.all_tasks:
                message = ""
                if task.status in {
                    "failed",
                    "unreclaimable",
                    "not_owned",
                    "skipped",
                }:
                    message = (
                        task.message
                        or task.download_error
                        or task.error_code
                        or f"兑换任务状态异常：{task.status}"
                    )
                elif task.status == "done" and not task.download_token:
                    message = (
                        task.download_error
                        or task.message
                        or (
                            "账号当前正常，未执行 401 找回"
                            if task.no_action
                            else "任务已完成但没有下载令牌"
                        )
                    )
                if message:
                    issue_count += 1
                    issues[cls._sanitize_redeem_message(message, card_codes)] += 1
        return issues, issue_count

    @staticmethod
    def _sanitize_redeem_message(message: str, card_codes: list[str]) -> str:
        sanitized = " ".join(message.split())
        for card_code in sorted(card_codes, key=len, reverse=True):
            sanitized = sanitized.replace(card_code, "[兑换码]")
        return sanitized[:240] or "兑换服务未提供具体原因"

    @staticmethod
    def _format_redeem_issues(issues: Counter[str]) -> str:
        top_issues = issues.most_common(3)
        parts = [f"{message}（{count} 项）" for message, count in top_issues]
        remaining = sum(issues.values()) - sum(count for _, count in top_issues)
        if remaining:
            parts.append(f"其他原因（{remaining} 项）")
        return "；".join(parts)

    @staticmethod
    def _batch_progress(result: BatchReclaimResult) -> float:
        total = max(result.total, result.tracked_tasks, len(result.all_tasks), 1)
        terminal = (
            result.done
            + result.failed
            + result.unreclaimable
            + result.not_owned
            + result.skipped
        )
        return max(0.0, min(1.0, terminal / total))
