from __future__ import annotations

import re
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator

CARD_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]$")


class JobInputBase(BaseModel):
    redeem_base_url: AnyHttpUrl
    card_codes: list[str] = Field(min_length=1, max_length=100)
    mode: Literal["all", "401"] = "all"

    @field_validator("card_codes")
    @classmethod
    def normalize_card_codes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            code = raw.strip()
            if not 4 <= len(code) <= 128 or not CARD_CODE_PATTERN.fullmatch(code):
                raise ValueError(f"无效的兑换码格式：{code or '<空>'}")
            if code not in seen:
                normalized.append(code)
                seen.add(code)
        if not normalized:
            raise ValueError("至少需要一个有效兑换码")
        return normalized


class StartImportJobRequest(JobInputBase):
    """Public job request; Sub2API credentials come from persistent config."""


class ImportJobRequest(JobInputBase):
    """Resolved internal request containing persisted Sub2API credentials."""

    sub2api_base_url: AnyHttpUrl
    sub2api_token: SecretStr = Field(min_length=1)
    verify_sub2api_tls: bool = True
    group_id: int | None = Field(default=None, gt=0)
    proxy_id: int | None = Field(default=None, gt=0)


class Sub2APIConfigUpdate(BaseModel):
    base_url: AnyHttpUrl
    access_token: SecretStr | None = None
    verify_tls: bool = True
    group_id: int | None = Field(default=None, gt=0)
    proxy_id: int | None = Field(default=None, gt=0)

    @field_validator("access_token", mode="before")
    @classmethod
    def normalize_optional_token(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class Sub2APIConfigResponse(BaseModel):
    configured: bool
    base_url: str | None = None
    has_token: bool = False
    verify_tls: bool = True
    group_id: int | None = None
    proxy_id: int | None = None
    updated_at: str | None = None


class Sub2APIGroupOption(BaseModel):
    id: int
    name: str
    platform: str = ""


class Sub2APIProxyOption(BaseModel):
    id: int
    name: str
    protocol: str = ""
    host: str = ""
    port: int = 0
    account_count: int = 0


class Sub2APIOptionsResponse(BaseModel):
    groups: list[Sub2APIGroupOption] = Field(default_factory=list)
    proxies: list[Sub2APIProxyOption] = Field(default_factory=list)


class Sub2API401Account(BaseModel):
    id: int
    name: str
    email: str | None = None
    platform: str
    type: str
    status: str
    error_message: str
    card_code: str | None = None


class Sub2API401ScanResponse(BaseModel):
    scanned: int
    detected_401: int
    recoverable: int
    missing_card_code: int
    unique_codes: int
    accounts: list[Sub2API401Account] = Field(default_factory=list)


class LocalAccountRecord(BaseModel):
    id: int
    sub2api_base_url: str
    sub2api_account_id: int
    email: str
    card_code: str
    platform: str
    last_operation: Literal["import", "recover_401"]
    last_status: Literal["success", "failed"]
    last_job_id: str
    last_message: str
    first_recorded_at: str
    updated_at: str


class LocalAccountListResponse(BaseModel):
    total: int
    items: list[LocalAccountRecord] = Field(default_factory=list)


class StartRecover401JobRequest(BaseModel):
    redeem_base_url: AnyHttpUrl
    account_ids: list[int] = Field(min_length=1, max_length=1000)

    @field_validator("account_ids")
    @classmethod
    def normalize_account_ids(cls, values: list[int]) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for account_id in values:
            if account_id <= 0:
                raise ValueError("账号 ID 必须为正整数")
            if account_id not in seen:
                normalized.append(account_id)
                seen.add(account_id)
        return normalized


class Recover401JobRequest(StartRecover401JobRequest):
    sub2api_base_url: AnyHttpUrl
    sub2api_token: SecretStr = Field(min_length=1)
    verify_sub2api_tls: bool = True


class JobEvent(BaseModel):
    time: str
    level: Literal["info", "success", "warning", "error"] = "info"
    message: str


class JobSnapshot(BaseModel):
    id: str
    status: Literal["queued", "running", "succeeded", "partial", "failed"]
    stage: str
    progress: int = Field(ge=0, le=100)
    message: str
    created_at: str
    updated_at: str
    summary: dict = Field(default_factory=dict)
    error: str | None = None
    events: list[JobEvent] = Field(default_factory=list)


class CreateJobResponse(BaseModel):
    job: JobSnapshot


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    sdk_version: str
