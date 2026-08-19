from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from redeem_api_sdk import SDK_VERSION

from .config_store import ConfigStore, ConfigStoreError
from .jobs import JobManager
from .ledger import AccountLedger, AccountLedgerError
from .models import (
    MAX_MANUAL_IMPORT_BYTES,
    CreateJobResponse,
    HealthResponse,
    ImportJobRequest,
    JobSnapshot,
    LocalAccountListResponse,
    ManualImportJobRequest,
    Recover401JobRequest,
    StartImportJobRequest,
    StartManualImportJobRequest,
    StartRecover401JobRequest,
    Sub2API401ScanResponse,
    Sub2APIConfigResponse,
    Sub2APIConfigUpdate,
    Sub2APIOptionsResponse,
)
from .sub2api import fetch_sub2api_401_accounts, fetch_sub2api_options

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIST = ROOT_DIR / "web" / "dist"


def create_app(
    manager: JobManager | None = None,
    config_store: ConfigStore | None = None,
    ledger: AccountLedger | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Account Import API",
        version="0.1.0",
        description="兑换额度、下载凭据并导入 Sub2API。",
    )
    account_ledger = ledger or AccountLedger()
    job_manager = manager or JobManager(ledger=account_ledger)
    persistent_config = config_store or ConfigStore()
    app.state.job_manager = job_manager
    app.state.config_store = persistent_config
    app.state.account_ledger = account_ledger

    origins = [
        value.strip()
        for value in os.getenv(
            "ACCOUNT_IMPORT_CORS_ORIGINS",
            os.getenv(
                "TEAM_IMPORT_CORS_ORIGINS",
                "http://localhost:5173,http://127.0.0.1:5173",
            ),
        ).split(",")
        if value.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        return HealthResponse(sdk_version=SDK_VERSION)

    @app.get(
        "/api/config/sub2api",
        response_model=Sub2APIConfigResponse,
        tags=["config"],
    )
    async def get_sub2api_config() -> Sub2APIConfigResponse:
        try:
            return persistent_config.public()
        except ConfigStoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put(
        "/api/config/sub2api",
        response_model=Sub2APIConfigResponse,
        tags=["config"],
    )
    async def save_sub2api_config(
        payload: Sub2APIConfigUpdate,
    ) -> Sub2APIConfigResponse:
        try:
            return persistent_config.save(payload)
        except ConfigStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get(
        "/api/config/sub2api/options",
        response_model=Sub2APIOptionsResponse,
        tags=["config"],
    )
    async def get_sub2api_options() -> Sub2APIOptionsResponse:
        try:
            sub2api = persistent_config.require()
            result = await fetch_sub2api_options(
                base_url=str(sub2api.base_url).rstrip("/"),
                token=sub2api.access_token.get_secret_value(),
                verify_tls=sub2api.verify_tls,
            )
            return Sub2APIOptionsResponse.model_validate(result)
        except ConfigStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get(
        "/api/sub2api/accounts/401",
        response_model=Sub2API401ScanResponse,
        tags=["sub2api"],
    )
    async def scan_sub2api_401_accounts(
        group_id: int = Query(gt=0),
    ) -> Sub2API401ScanResponse:
        try:
            sub2api = persistent_config.require()
            result = await fetch_sub2api_401_accounts(
                base_url=str(sub2api.base_url).rstrip("/"),
                token=sub2api.access_token.get_secret_value(),
                verify_tls=sub2api.verify_tls,
                group_id=group_id,
            )
            return Sub2API401ScanResponse.model_validate(result)
        except ConfigStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get(
        "/api/records/accounts",
        response_model=LocalAccountListResponse,
        tags=["records"],
    )
    async def list_local_account_records(
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> LocalAccountListResponse:
        try:
            result = await asyncio.to_thread(
                account_ledger.list_accounts, limit=limit, offset=offset
            )
            return LocalAccountListResponse.model_validate(result)
        except AccountLedgerError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post(
        "/api/jobs", response_model=CreateJobResponse, status_code=202, tags=["jobs"]
    )
    async def create_job(payload: StartImportJobRequest) -> CreateJobResponse:
        try:
            sub2api = persistent_config.require()
        except ConfigStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        resolved = ImportJobRequest(
            **payload.model_dump(mode="json"),
            sub2api_base_url=sub2api.base_url,
            sub2api_token=sub2api.access_token,
            verify_sub2api_tls=sub2api.verify_tls,
            group_id=sub2api.group_id,
        )
        record = job_manager.create(resolved)
        return CreateJobResponse(job=record.snapshot())

    @app.post(
        "/api/jobs/recover-401",
        response_model=CreateJobResponse,
        status_code=202,
        tags=["jobs"],
    )
    async def create_recover_401_job(
        payload: StartRecover401JobRequest,
    ) -> CreateJobResponse:
        try:
            sub2api = persistent_config.require()
        except ConfigStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        resolved = Recover401JobRequest(
            **payload.model_dump(mode="json"),
            sub2api_base_url=sub2api.base_url,
            sub2api_token=sub2api.access_token,
            verify_sub2api_tls=sub2api.verify_tls,
        )
        record = job_manager.create_recovery(resolved)
        return CreateJobResponse(job=record.snapshot())

    @app.post(
        "/api/jobs/manual-import",
        response_model=CreateJobResponse,
        status_code=202,
        tags=["jobs"],
    )
    async def create_manual_import_job(
        payload: StartManualImportJobRequest,
    ) -> CreateJobResponse:
        payload_size = len(
            json.dumps(
                payload.payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if payload_size > MAX_MANUAL_IMPORT_BYTES:
            raise HTTPException(
                status_code=413,
                detail="account.json 不能超过 5 MiB",
            )
        try:
            sub2api = persistent_config.require()
        except ConfigStoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        resolved = ManualImportJobRequest(
            **payload.model_dump(mode="json"),
            sub2api_base_url=sub2api.base_url,
            sub2api_token=sub2api.access_token,
            verify_sub2api_tls=sub2api.verify_tls,
            group_id=sub2api.group_id,
        )
        record = job_manager.create_manual_import(resolved)
        return CreateJobResponse(job=record.snapshot())

    @app.get("/api/jobs/{job_id}", response_model=JobSnapshot, tags=["jobs"])
    async def get_job(job_id: str) -> JobSnapshot:
        record = job_manager.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        return record.snapshot()

    if WEB_DIST.is_dir():
        assets = WEB_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = (WEB_DIST / path).resolve()
            if candidate.is_relative_to(WEB_DIST.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
