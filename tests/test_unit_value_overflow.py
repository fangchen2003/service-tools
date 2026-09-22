"""Oversized JSON numbers are invalid reference values, never server errors."""
import pytest

from app import main
from app.policy import _unit_value
from test_generation_integration import FakeState, image_body, encoding_body, post


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/ai/encode-vibe", "/ai/generate-image", "/ai/generate-image-stream"])
@pytest.mark.parametrize("sign", [1, -1])
async def test_huge_reference_integer_returns_400_without_dispatch(monkeypatch, path, sign):
    state = FakeState()
    monkeypatch.setattr(main, "STATE", state)
    value = sign * 10 ** 1000
    if path.endswith("encode-vibe"):
        body = encoding_body() | {"informationExtracted": value}
    else:
        body = image_body(precise=1)
        body["parameters"]["director_reference_strength_values"] = [value]
    response = await post(path, body)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert not state.nai.calls and not state.db.charges
    assert state.global_active == 0 and not state.image_budget_lock.locked()


def test_unit_value_keeps_normal_boundaries_and_rejects_nonfinite_or_wrong_types():
    for value in (0, 1, 0.0, 1.0, .5):
        assert _unit_value(value)
    for value in (-1, 2, -.1, 1.1, True, False, None, "0.5",
                  float("nan"), float("inf"), -float("inf"), 10 ** 1000, -(10 ** 1000)):
        assert not _unit_value(value)
