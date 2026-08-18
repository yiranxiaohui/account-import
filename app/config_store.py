from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, SecretStr, ValidationError

from .models import Sub2APIConfigResponse, Sub2APIConfigUpdate

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "data" / "config.json"


class ConfigStoreError(RuntimeError):
    """Raised when persistent configuration cannot be read or written."""


class StoredSub2APIConfig(BaseModel):
    base_url: AnyHttpUrl
    access_token: SecretStr
    verify_tls: bool = True
    group_id: int | None = None
    updated_at: str


class StoredConfig(BaseModel):
    version: int = 1
    sub2api: StoredSub2APIConfig


class ConfigStore:
    """Atomic JSON-backed storage for Sub2API connection settings."""

    def __init__(self, path: Path | str | None = None) -> None:
        configured_path = (
            path
            or os.getenv("ACCOUNT_IMPORT_CONFIG_FILE")
            or os.getenv("TEAM_IMPORT_CONFIG_FILE")
            or DEFAULT_CONFIG_PATH
        )
        self.path = Path(configured_path).expanduser().resolve()
        self._lock = threading.RLock()

    def public(self) -> Sub2APIConfigResponse:
        config = self.load()
        if config is None:
            return Sub2APIConfigResponse(configured=False)
        return Sub2APIConfigResponse(
            configured=True,
            base_url=str(config.base_url).rstrip("/"),
            has_token=bool(config.access_token.get_secret_value()),
            verify_tls=config.verify_tls,
            group_id=config.group_id,
            updated_at=config.updated_at,
        )

    def require(self) -> StoredSub2APIConfig:
        config = self.load()
        if config is None or not config.access_token.get_secret_value():
            raise ConfigStoreError("请先保存 Sub2API 地址和管理员凭据")
        return config

    def load(self) -> StoredSub2APIConfig | None:
        with self._lock:
            if not self.path.exists():
                return None
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return StoredConfig.model_validate(raw).sub2api
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                raise ConfigStoreError(f"读取 Sub2API 配置失败：{exc}") from exc

    def save(self, update: Sub2APIConfigUpdate) -> Sub2APIConfigResponse:
        with self._lock:
            existing = self.load()
            next_token = (
                update.access_token.get_secret_value().strip()
                if update.access_token is not None
                else ""
            )
            if not next_token and existing is not None:
                next_token = existing.access_token.get_secret_value()
            if not next_token:
                raise ConfigStoreError(
                    "首次保存时必须提供管理员 API Key 或 Access Token"
                )

            stored = StoredConfig(
                sub2api=StoredSub2APIConfig(
                    base_url=update.base_url,
                    access_token=next_token,
                    verify_tls=update.verify_tls,
                    group_id=update.group_id,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
            self._write_atomic(stored)
            return self.public()

    def _write_atomic(self, config: StoredConfig) -> None:
        parent_exists = self.path.parent.exists()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_exists:
                os.chmod(self.path.parent, 0o700)

            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            file_descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                    serialized = config.model_dump(mode="json")
                    serialized["sub2api"]["access_token"] = (
                        config.sub2api.access_token.get_secret_value()
                    )
                    json.dump(
                        serialized,
                        handle,
                        ensure_ascii=False,
                        indent=2,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                os.chmod(self.path, 0o600)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise ConfigStoreError(f"保存 Sub2API 配置失败：{exc}") from exc
