"""Legacy and V5 High allow up to 28 steps (no real upstream)."""
import pytest

from app.policy import estimate_image_cost
from test_generation_integration import image_body, post, state
from test_image_stream_routes import StreamingNai


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [
    "nai-diffusion-4-5-full", "nai-diffusion-4-5-curated", "nai-diffusion-4-full",
    "nai-diffusion-3", "nai-diffusion-furry-3", "nai-diffusion-2", "safe-diffusion",
])
@pytest.mark.parametrize("steps,expected", [(23, 23), (28, 28), (50, 28)])
@pytest.mark.parametrize("streaming", [False, True])
async def test_legacy_free_steps_preserved_up_to_28(state, model, steps, expected, streaming):
    if streaming:
        state.nai = StreamingNai()
    body = image_body(steps=steps)
    body["model"] = model
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""), body)
    assert response.status_code == 200
    sent = state.nai.calls[0][1 if streaming else 2]
    assert sent["parameters"]["steps"] == expected
    assert estimate_image_cost(sent) == {"anlas": 0, "v5": 0}
    assert state.nai.calls[0][2 if streaming else 3]["requires_anlas"] is False
    assert state.db.charges[0][1]["legacy_free_images"] == 1
    assert state.db.charges[0][1]["anlas"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["nai-diffusion-5-full", "nai-diffusion-5-curated"])
@pytest.mark.parametrize("steps,expected", [(23, 23), (28, 28), (50, 28)])
@pytest.mark.parametrize("streaming", [False, True])
async def test_v5_high_preserves_up_to_28_steps(state, model, steps, expected, streaming):
    state.image_reservations = {}
    if streaming:
        state.nai = StreamingNai()
    body = image_body(steps=steps)
    body["model"] = model
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""), body)
    assert response.status_code == 200
    sent = state.nai.calls[0][1 if streaming else 2]
    assert sent["parameters"]["steps"] == expected
    assert state.nai.calls[0][2 if streaming else 3]["requires_anlas"] is False
    assert state.db.charges[0][1]["anlas"] == 0
    assert state.db.charges[0][1]["v5"] == 1


@pytest.mark.parametrize("configured,expected", [("", 28), ("100", 28), ("20", 20)])
def test_config_allows_28_legacy_steps_with_safe_upper_bound(configured, expected):
    import os
    import subprocess
    import sys
    output = subprocess.check_output(
        [sys.executable, "-c", "from app.config import load_settings; print(load_settings().max_steps)"],
        env={**os.environ, "MAX_STEPS": configured}, text=True,
    )
    assert int(output.strip()) == expected
