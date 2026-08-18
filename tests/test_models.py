import pytest
from pydantic import ValidationError

from app.models import StartImportJobRequest


def make_request(**overrides):
    values = {
        "redeem_base_url": "https://redeem.example.com",
        "card_codes": ["RCL-AAAA-BBBB"],
    }
    values.update(overrides)
    return StartImportJobRequest(**values)


def test_card_codes_are_trimmed_and_deduplicated():
    request = make_request(
        card_codes=[" RCL-AAAA-BBBB ", "RCL-AAAA-BBBB", "RCL-CCCC-DDDD"]
    )

    assert request.card_codes == ["RCL-AAAA-BBBB", "RCL-CCCC-DDDD"]


def test_import_proxy_must_be_a_positive_id():
    assert make_request(proxy_id=23).proxy_id == 23
    with pytest.raises(ValidationError):
        make_request(proxy_id=0)


@pytest.mark.parametrize(
    "code", ["", "abc", "contains space", "https://example.com", "-broken-"]
)
def test_invalid_card_code_is_rejected(code: str):
    with pytest.raises(ValidationError):
        make_request(card_codes=[code])
