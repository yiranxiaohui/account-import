import requests

from redeem_api_sdk import RedeemClient


def test_health_check_allows_callers_to_override_the_default_timeout(monkeypatch):
    observed: dict[str, float] = {}

    def fake_post(url, *, json, timeout):
        observed["timeout"] = timeout
        raise requests.ReadTimeout("timed out")

    monkeypatch.setattr("redeem_api_sdk.requests.post", fake_post)
    result = RedeemClient("https://redeem.example.com", timeout=45).health_check(
        ["RCL-AAAA-BBBB"], timeout=3
    )

    assert observed["timeout"] == 3.0
    assert result.ok is False
    assert "timed out" in result.error
