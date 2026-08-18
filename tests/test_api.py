import httpx
import pytest

from app.config_store import ConfigStore
from app.jobs import JobManager, JobRecord
from app.ledger import AccountLedger
from app.main import create_app
from app.models import Sub2APIConfigUpdate


@pytest.mark.asyncio
async def test_health_endpoint_reports_sdk_version():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["sdk_version"]


@pytest.mark.asyncio
async def test_unknown_job_returns_404():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/jobs/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "任务不存在或已过期"


@pytest.mark.asyncio
async def test_invalid_job_request_returns_validation_error():
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/jobs",
            json={
                "redeem_base_url": "https://redeem.example.com",
                "card_codes": ["bad code"],
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sub2api_config_api_persists_and_redacts_token(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    transport = httpx.ASGITransport(app=create_app(config_store=store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get("/api/config/sub2api")
        saved = await client.put(
            "/api/config/sub2api",
            json={
                "base_url": "https://sub2api.example.com",
                "access_token": "admin-token",
                "verify_tls": True,
                "group_id": 7,
                "proxy_id": 9,
            },
        )
        loaded = await client.get("/api/config/sub2api")

    assert initial.json()["configured"] is False
    assert saved.status_code == 200
    assert loaded.json()["configured"] is True
    assert loaded.json()["has_token"] is True
    assert loaded.json()["group_id"] == 7
    assert "proxy_id" not in loaded.json()
    assert "access_token" not in loaded.text
    assert "admin-token" not in loaded.text


@pytest.mark.asyncio
async def test_import_job_uses_proxy_selected_for_that_request(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    store.save(
        Sub2APIConfigUpdate(
            base_url="https://sub2api.example.com",
            access_token="admin-token",
            group_id=7,
        )
    )
    manager = JobManager()
    captured = {}

    def fake_create(request):
        captured["request"] = request
        return JobRecord(id="job-1")

    monkeypatch.setattr(manager, "create", fake_create)
    transport = httpx.ASGITransport(app=create_app(manager=manager, config_store=store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/jobs",
            json={
                "redeem_base_url": "https://redeem.example.com",
                "card_codes": ["RCL-AAAA-BBBB"],
                "mode": "all",
                "proxy_id": 23,
            },
        )

    assert response.status_code == 202
    request = captured["request"]
    assert request.group_id == 7
    assert request.proxy_id == 23


@pytest.mark.asyncio
async def test_job_requires_persisted_sub2api_config(tmp_path):
    store = ConfigStore(tmp_path / "missing.json")
    transport = httpx.ASGITransport(app=create_app(config_store=store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/jobs",
            json={
                "redeem_base_url": "https://redeem.example.com",
                "card_codes": ["RCL-AAAA-BBBB"],
                "mode": "all",
            },
        )

    assert response.status_code == 409
    assert "Sub2API" in response.json()["detail"]


@pytest.mark.asyncio
async def test_scan_401_endpoint_uses_persisted_sub2api_config(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path / "config.json")
    store.save(
        Sub2APIConfigUpdate(
            base_url="https://sub2api.example.com",
            access_token="admin-token",
        )
    )

    async def fake_scan(**kwargs):
        assert kwargs["base_url"] == "https://sub2api.example.com"
        assert kwargs["token"] == "admin-token"
        return {
            "scanned": 5,
            "detected_401": 1,
            "recoverable": 1,
            "missing_card_code": 0,
            "unique_codes": 1,
            "accounts": [
                {
                    "id": 8,
                    "name": "owner@example.com",
                    "email": "owner@example.com",
                    "platform": "openai",
                    "type": "oauth",
                    "status": "error",
                    "error_message": "401",
                    "card_code": "RCL-AAAA-BBBB",
                }
            ],
        }

    monkeypatch.setattr("app.main.fetch_sub2api_401_accounts", fake_scan)
    transport = httpx.ASGITransport(app=create_app(config_store=store))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/sub2api/accounts/401")

    assert response.status_code == 200
    assert response.json()["recoverable"] == 1
    assert response.json()["accounts"][0]["card_code"] == "RCL-AAAA-BBBB"


@pytest.mark.asyncio
async def test_local_account_records_endpoint_reads_sqlite_ledger(tmp_path):
    ledger = AccountLedger(tmp_path / "account-import.db")
    ledger.record(
        operation="import",
        status="success",
        job_id="job-1",
        sub2api_base_url="https://sub2api.example.com",
        sub2api_account_id=88,
        email="owner@example.com",
        card_code="RCL-AAAA-BBBB",
        platform="openai",
        message="账号已创建",
    )
    transport = httpx.ASGITransport(app=create_app(ledger=ledger))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/records/accounts?limit=10")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["sub2api_account_id"] == 88
    assert response.json()["items"][0]["email"] == "owner@example.com"
