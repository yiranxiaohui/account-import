import json
import stat

from app.config_store import ConfigStore
from app.models import Sub2APIConfigUpdate


def test_sub2api_config_survives_new_store_instance_and_is_private(tmp_path):
    config_path = tmp_path / "data" / "config.json"
    store = ConfigStore(config_path)

    response = store.save(
        Sub2APIConfigUpdate(
            base_url="https://sub2api.example.com",
            access_token="super-secret-token",
            verify_tls=True,
            group_id=12,
        )
    )

    assert response.configured is True
    assert response.has_token is True
    assert "token" not in response.model_dump()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    reloaded = ConfigStore(config_path).require()
    assert str(reloaded.base_url).rstrip("/") == "https://sub2api.example.com"
    assert reloaded.access_token.get_secret_value() == "super-secret-token"
    assert reloaded.group_id == 12


def test_saving_without_new_token_preserves_existing_secret(tmp_path):
    config_path = tmp_path / "config.json"
    store = ConfigStore(config_path)
    store.save(
        Sub2APIConfigUpdate(
            base_url="http://localhost:8080",
            access_token="original-token",
        )
    )

    store.save(
        Sub2APIConfigUpdate(
            base_url="http://localhost:9090",
            access_token=None,
            verify_tls=False,
            group_id=55,
        )
    )

    reloaded = ConfigStore(config_path).require()
    assert reloaded.access_token.get_secret_value() == "original-token"
    assert str(reloaded.base_url).rstrip("/") == "http://localhost:9090"
    assert reloaded.verify_tls is False
    assert reloaded.group_id == 55

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["sub2api"]["access_token"] == "original-token"


def test_legacy_global_proxy_setting_is_ignored(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sub2api": {
                    "base_url": "https://sub2api.example.com",
                    "access_token": "admin-token",
                    "verify_tls": True,
                    "group_id": 12,
                    "proxy_id": 34,
                    "updated_at": "2026-08-18T00:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )

    store = ConfigStore(config_path)

    assert store.require().group_id == 12
    assert "proxy_id" not in store.public().model_dump()
