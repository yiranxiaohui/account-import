import json

import pytest

from app.jobs import JobManager
from app.ledger import AccountLedger
from app.models import ImportJobRequest, Recover401JobRequest
from redeem_api_sdk import BatchReclaimResult, HealthCheckResult, ReclaimTask


class FakeRedeemClient:
    def __init__(self, base_url: str, timeout: int):
        self.base_url = base_url
        self.timeout = timeout

    def health_check(self, card_codes: list[str]) -> HealthCheckResult:
        return HealthCheckResult(ok=True, total=1, healthy=1)

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
