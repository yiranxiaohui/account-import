import json

import pytest

from app.jobs import JobManager
from app.ledger import AccountLedger
from app.models import ImportJobRequest, ManualImportJobRequest, Recover401JobRequest
from redeem_api_sdk import BatchReclaimResult, HealthCheckResult, ReclaimTask


class FakeRedeemClient:
    def __init__(self, base_url: str, timeout: int):
        self.base_url = base_url
        self.timeout = timeout

    def health_check(
        self, card_codes: list[str], timeout: float | None = None
    ) -> HealthCheckResult:
        raise AssertionError(
            "import jobs should submit directly without a health precheck"
        )

    def batch_reclaim(self, card_codes: list[str], mode: str) -> BatchReclaimResult:
        return self._result()

    def refresh_progress(self, card_codes: list[str]) -> BatchReclaimResult:
        return self._result()

    def download(self, order_no: str, token: str) -> bytes:
        return json.dumps(
            {
                "proxies": [],
                "accounts": [
                    {
                        "name": "Imported account",
                        "platform": "openai",
                        "type": "oauth",
                        "credentials": {
                            "access_token": "access-token",
                            "email": "person@example.com",
                        },
                    }
                ],
            }
        ).encode()

    @staticmethod
    def _result() -> BatchReclaimResult:
        task = ReclaimTask(
            card_code="RCL-AAAA-BBBB",
            order_no="order-1",
            resource_uid="resource-1",
            status="done",
            download_token="download-token",
        )
        return BatchReclaimResult(
            ok=True, total=1, done=1, tracked_tasks=1, all_tasks=[task]
        )


@pytest.mark.asyncio
async def test_job_runs_the_full_download_and_import_pipeline(monkeypatch, tmp_path):
    async def fake_import(**kwargs):
        assert kwargs["token"] == "admin-token"
        assert len(kwargs["payload"]["accounts"]) == 1
        assert kwargs["payload"]["accounts"][0]["name"] == "person@example.com"
        assert kwargs["payload"]["accounts"][0]["notes"] == "RCL-AAAA-BBBB"
        assert kwargs["group_id"] == 17
        assert kwargs["proxy_id"] == 23
        return {
            "proxy_created": 0,
            "proxy_reused": 0,
            "proxy_failed": 0,
            "account_created": 1,
            "account_failed": 0,
            "results": [
                {
                    "id": 101,
                    "name": "person@example.com",
                    "success": True,
                    "error": "",
                }
            ],
        }

    monkeypatch.setattr("app.jobs.RedeemClient", FakeRedeemClient)
    monkeypatch.setattr("app.jobs.import_to_sub2api", fake_import)
    ledger = AccountLedger(tmp_path / "account-import.db")
    manager = JobManager(ledger=ledger)
    request = ImportJobRequest(
        redeem_base_url="https://redeem.example.com",
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
        card_codes=["RCL-AAAA-BBBB"],
        group_id=17,
        proxy_id=23,
    )

    record = manager.create(request)
    assert record.task is not None
    await record.task

    assert record.status == "succeeded"
    assert record.progress == 100
    assert record.summary["download"]["accounts"] == 1
    assert record.summary["import"]["account_created"] == 1
    local_records = ledger.list_accounts()
    assert local_records["total"] == 1
    assert local_records["items"][0]["sub2api_account_id"] == 101
    assert local_records["items"][0]["card_code"] == "RCL-AAAA-BBBB"


@pytest.mark.asyncio
async def test_manual_json_job_imports_with_selected_proxy_and_records_account(
    monkeypatch, tmp_path
):
    async def fake_import(**kwargs):
        assert kwargs["token"] == "admin-token"
        assert kwargs["group_id"] == 17
        assert kwargs["proxy_id"] == 23
        assert kwargs["payload"]["proxies"] == [
            {"name": "embedded-proxy", "host": "ignored.example.com"}
        ]
        return {
            "proxy_created": 0,
            "proxy_reused": 0,
            "proxy_failed": 0,
            "account_created": 1,
            "account_failed": 0,
            "results": [
                {
                    "id": 202,
                    "name": "owner@example.com",
                    "success": True,
                    "error": "",
                }
            ],
        }

    monkeypatch.setattr("app.jobs.import_to_sub2api", fake_import)
    ledger = AccountLedger(tmp_path / "account-import.db")
    manager = JobManager(ledger=ledger)
    request = ManualImportJobRequest(
        filename="account.json",
        payload={
            "proxies": [{"name": "embedded-proxy", "host": "ignored.example.com"}],
            "accounts": [
                {
                    "name": "owner@example.com",
                    "notes": "RCL-MANUAL-TEST",
                    "platform": "openai",
                    "type": "oauth",
                    "credentials": {
                        "access_token": "secret",
                        "email": "owner@example.com",
                    },
                }
            ],
        },
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
        group_id=17,
        proxy_id=23,
    )

    record = manager.create_manual_import(request)
    assert record.task is not None
    await record.task

    assert record.status == "succeeded"
    assert record.summary["operation"] == "manual_import"
    assert record.summary["manual_file"] == {
        "filename": "account.json",
        "accounts": 1,
        "embedded_proxies": 1,
    }
    assert any(
        "已忽略文件内 1 个代理定义" in event["message"] for event in record.events
    )
    local_records = ledger.list_accounts()
    assert local_records["total"] == 1
    assert local_records["items"][0]["last_operation"] == "manual_import"
    assert local_records["items"][0]["card_code"] == "RCL-MANUAL-TEST"


@pytest.mark.asyncio
async def test_manual_json_job_reports_invalid_account_payload():
    manager = JobManager()
    request = ManualImportJobRequest(
        filename="account.json",
        payload={"unexpected": True},
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
    )

    record = manager.create_manual_import(request)
    assert record.task is not None
    await record.task

    assert record.status == "failed"
    assert record.stage == "failed"
    assert "accounts" in (record.error or "")


@pytest.mark.asyncio
async def test_recovery_job_matches_by_code_and_email_then_updates_in_place(
    monkeypatch, tmp_path
):
    async def fake_scan(**kwargs):
        assert kwargs["token"] == "admin-token"
        return {
            "scanned": 2,
            "detected_401": 1,
            "recoverable": 1,
            "missing_card_code": 0,
            "unique_codes": 1,
            "accounts": [
                {
                    "id": 71,
                    "name": "person@example.com",
                    "email": "person@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "error",
                    "error_message": "Authentication failed (401)",
                    "card_code": "RCL-AAAA-BBBB",
                }
            ],
        }

    updated: list[dict] = []

    async def fake_replace(**kwargs):
        updated.append(kwargs)
        return {"id": kwargs["account_id"], "status": "active"}

    monkeypatch.setattr("app.jobs.RedeemClient", FakeRedeemClient)
    monkeypatch.setattr("app.jobs.fetch_sub2api_401_accounts", fake_scan)
    monkeypatch.setattr("app.jobs.replace_sub2api_account_credentials", fake_replace)
    ledger = AccountLedger(tmp_path / "account-import.db")
    manager = JobManager(ledger=ledger)
    request = Recover401JobRequest(
        redeem_base_url="https://redeem.example.com",
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
        account_ids=[71],
    )

    record = manager.create_recovery(request)
    assert record.task is not None
    await record.task

    assert record.status == "succeeded"
    assert record.summary["operation"] == "recover_401"
    assert record.summary["recovery"]["updated"] == 1
    assert updated[0]["account_id"] == 71
    assert updated[0]["card_code"] == "RCL-AAAA-BBBB"
    assert updated[0]["account"]["name"] == "person@example.com"
    local_records = ledger.list_accounts()
    assert local_records["total"] == 1
    assert local_records["items"][0]["last_operation"] == "recover_401"
    assert local_records["items"][0]["sub2api_account_id"] == 71


@pytest.mark.asyncio
async def test_empty_batch_reports_sanitized_card_errors_without_health_precheck(
    monkeypatch,
):
    class InvalidCardRedeemClient:
        def __init__(self, base_url: str, timeout: int):
            self.base_url = base_url
            self.timeout = timeout

        def health_check(
            self, card_codes: list[str], timeout: float | None = None
        ) -> HealthCheckResult:
            raise AssertionError(
                "import jobs should submit directly without a health precheck"
            )

        def batch_reclaim(self, card_codes: list[str], mode: str) -> BatchReclaimResult:
            return self._result(card_codes)

        def refresh_progress(self, card_codes: list[str]) -> BatchReclaimResult:
            return self._result(card_codes)

        @staticmethod
        def _result(card_codes: list[str]) -> BatchReclaimResult:
            return BatchReclaimResult(
                ok=True,
                requested_cards=len(card_codes),
                valid_cards=0,
                cards=[
                    {
                        "card_code": card_code,
                        "card_status": "card_not_found",
                        "error": f"兑换码 {card_code} 无效",
                        "tasks": [],
                    }
                    for card_code in card_codes
                ],
            )

    monkeypatch.setattr("app.jobs.RedeemClient", InvalidCardRedeemClient)
    manager = JobManager()
    request = ImportJobRequest(
        redeem_base_url="https://redeem.example.com",
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
        card_codes=["RCL-AAAA-BBBB", "RCL-CCCC-DDDD"],
    )

    record = manager.create(request)
    assert record.task is not None
    await record.task

    assert record.status == "failed"
    assert record.progress == 10
    assert record.error == ("没有取得可下载的额度文件：兑换码 [兑换码] 无效（2 项）")
    assert "RCL-AAAA-BBBB" not in record.error
    assert "RCL-CCCC-DDDD" not in record.error
    assert record.summary["redeem"]["requested_cards"] == 2
    assert record.summary["redeem"]["valid_cards"] == 0
    assert record.summary["redeem"]["issue_count"] == 2
    assert record.summary["redeem"]["issues"] == [
        {"message": "兑换码 [兑换码] 无效", "count": 2}
    ]
    assert any(
        event["level"] == "warning" and "（2 项）" in event["message"]
        for event in record.events
    )


@pytest.mark.asyncio
async def test_valid_cards_without_resources_retry_once_and_report_the_reason(
    monkeypatch,
):
    batch_calls = 0

    class EmptyValidRedeemClient:
        def __init__(self, base_url: str, timeout: int):
            self.base_url = base_url
            self.timeout = timeout

        def batch_reclaim(self, card_codes: list[str], mode: str) -> BatchReclaimResult:
            nonlocal batch_calls
            batch_calls += 1
            return self._result(card_codes)

        def refresh_progress(self, card_codes: list[str]) -> BatchReclaimResult:
            return self._result(card_codes)

        @staticmethod
        def _result(card_codes: list[str]) -> BatchReclaimResult:
            return BatchReclaimResult(
                ok=True,
                requested_cards=len(card_codes),
                valid_cards=len(card_codes),
                scanned_resources=0,
                distinct_resources=0,
                cards=[
                    {
                        "card_code": card_code,
                        "card_status": "valid",
                        "tasks": [],
                    }
                    for card_code in card_codes
                ],
            )

    monkeypatch.setenv("ACCOUNT_IMPORT_EMPTY_RESULT_RETRY_DELAY", "0")
    monkeypatch.setattr("app.jobs.RedeemClient", EmptyValidRedeemClient)
    manager = JobManager()
    request = ImportJobRequest(
        redeem_base_url="https://redeem.example.com",
        sub2api_base_url="http://sub2api.example.com",
        sub2api_token="admin-token",
        card_codes=["RCL-AAAA-BBBB", "RCL-CCCC-DDDD"],
    )

    record = manager.create(request)
    assert record.task is not None
    await record.task

    assert batch_calls == 2
    assert record.status == "failed"
    assert record.error == (
        "没有取得可下载的额度文件：兑换码有效，但兑换服务未返回关联账号（2 项）"
    )
    assert record.summary["redeem"]["requested_cards"] == 2
    assert record.summary["redeem"]["valid_cards"] == 2
    assert record.summary["redeem"]["scanned_resources"] == 0
    assert record.summary["redeem"]["distinct_resources"] == 0
    assert record.summary["redeem"]["issue_count"] == 2
    assert any(
        event["level"] == "warning" and "自动重试" in event["message"]
        for event in record.events
    )
