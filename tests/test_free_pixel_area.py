"""Free pixel-area policy shared by quotation and both generation routes.

All dispatches use the local fake upstream; no image credits are spent.
"""
import copy

import pytest

from app.policy import clamp_image_params, estimate_image_cost
from test_generation_integration import image_body, post
from test_image_quotes import quote
from test_v5_effort import effort_env


@pytest.mark.parametrize("model", ["nai-diffusion-4-5-full", "nai-diffusion-5-full",
                                   "nai-diffusion-5-curated", "nai-diffusion-5-full-medium"])
@pytest.mark.parametrize("size", [(896, 1152), (1152, 896), (512, 2048), (2048, 512),
                                  (1024, 1024), (832, 1216)])
def test_free_area_custom_dimensions_are_not_snapped(model, size):
    body = {**image_body(width=size[0], height=size[1]), "model": model}
    original = copy.deepcopy(body)
    out, notes, error = clamp_image_params(
        body, max_pixels=1024 * 1024, max_steps=28, allow_img2img=False)
    assert error is None
    assert out == original
    assert notes == []
    assert estimate_image_cost(out)["anlas"] == 0
    assert body == original


@pytest.mark.parametrize("model", ["nai-diffusion-4-5-full", "nai-diffusion-5-full"])
@pytest.mark.parametrize("site_limit", [1024 * 1024, 2 * 1024 * 1024, 512 * 512])
def test_area_above_official_or_site_limit_stays_free_after_clamp(model, site_limit):
    body = {**image_body(width=1024, height=1088), "model": model}
    out, notes, error = clamp_image_params(
        body, max_pixels=site_limit, max_steps=28, allow_img2img=False)
    assert error is None and notes
    p = out["parameters"]
    assert p["width"] * p["height"] <= min(1024 * 1024, site_limit)
    assert p["width"] % 64 == p["height"] % 64 == 0
    assert estimate_image_cost(out)["anlas"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("model", ["nai-diffusion-4-5-full", "nai-diffusion-5-full",
                                   "nai-diffusion-5-curated", "nai-diffusion-5-full-medium"])
@pytest.mark.parametrize("size", [(896, 1152), (512, 2048), (2048, 512)])
async def test_area_quote_and_execution_keep_dimensions(effort_env, streaming, model, size):
    body = {**image_body(width=size[0], height=size[1]), "model": model}
    response = await quote(body)
    assert response.status_code == 200, response.text
    quoted = response.json()
    effective = quoted["effective_parameters"]
    assert (effective["width"], effective["height"]) == size
    assert quoted["cost_limit"]["anlas"] == 0
    assert effort_env.calls == []  # Quotes never dispatch or consume an image.
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""),
                          {**body, "cost_limit": quoted["cost_limit"]})
    assert response.status_code == 200, response.text
    sent, = effort_env.calls
    assert (sent["parameters"]["width"], sent["parameters"]["height"]) == size
    assert sent["model"] == quoted["effective_model"]
    counter = await effort_env.state.db.get_counter(1, effort_env.state.day())
    assert counter["anlas"] == 0
    assert counter["v5"] == quoted["cost_limit"]["v5"]
    assert counter["images"] == 1
