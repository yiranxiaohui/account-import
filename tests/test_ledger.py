import sqlite3
import stat

from app.ledger import AccountLedger


def test_sqlite_ledger_persists_accounts_and_audit_events_without_credentials(
    tmp_path,
):
    database_path = tmp_path / "data" / "account-import.db"
    ledger = AccountLedger(database_path)

    ledger.record(
        operation="import",
        status="success",
        job_id="job-import",
        sub2api_base_url="https://sub2api.example.com/",
        sub2api_account_id=41,
        email="owner@example.com",
        card_code="RCL-AAAA-BBBB",
        platform="openai",
        message="账号已创建",
    )
    ledger.record(
        operation="import",
        status="failed",
        job_id="job-failed",
        sub2api_base_url="https://sub2api.example.com",
        sub2api_account_id=None,
        email="failed@example.com",
        card_code="RCL-CCCC-DDDD",
        platform="openai",
        message="创建失败",
    )
    ledger.record(
        operation="recover_401",
        status="success",
        job_id="job-recover",
        sub2api_base_url="https://sub2api.example.com",
        sub2api_account_id=41,
        email="owner@example.com",
        card_code="RCL-AAAA-BBBB",
        platform="openai",
        message="401 已恢复",
    )

    reloaded = AccountLedger(database_path)
    records = reloaded.list_accounts()
    events = reloaded.list_events()

    assert records["total"] == 1
    assert records["items"][0]["sub2api_account_id"] == 41
    assert records["items"][0]["last_operation"] == "recover_401"
    assert records["items"][0]["last_job_id"] == "job-recover"
    assert len(events) == 3
    assert events[0]["operation"] == "recover_401"
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

    with sqlite3.connect(database_path) as connection:
        event_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(account_events)")
        }
    assert "credentials" not in event_columns
    assert "access_token" not in event_columns
