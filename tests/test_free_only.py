"""Ordinary generation must never select a paid upstream path (all upstreams fake)."""
import copy

import pytest

from app import main
from app.policy import clamp_image_params, estimate_image_cost
from test_generation_integration import image_body, post, state


@pytest.mark.asyncio
@pytest.mark.parametrize("safe_clamp", [False, True])
@pytest.mark.parametrize("allow_anlas", [False, True])
async def test_all_ordinary_generation_is_clamped_free(state, safe_clamp, allow_anlas):
    state.settings.safe_clamp = safe_clamp
    state.db.keys["fixture-1"]["allow_anlas"] = allow_anlas
    body = image_body(width=2048, height=2048, steps=50, n_samples=4, sm=True, sm_dyn=True)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200
    sent = state.nai.calls[0][2]
    assert estimate_image_cost(sent) == {"anlas": 0, "v5": 0}
    assert sent["parameters"]["steps"] == 28
    assert sent["parameters"]["n_samples"] == 1
    assert state.nai.calls[0][3]["requires_anlas"] is False
    assert state.db.charges[0][1]["anlas"] == 0
    assert state.db.charges[0][1]["legacy_free_images"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_anlas", [False, True])
async def test_paid_features_refused_without_numeric_budget_lookup(state, allow_anlas):
    key = state.db.keys["fixture-1"]
    key["allow_anlas"] = allow_anlas

    async def obsolete(*args, **kwargs):
        raise AssertionError("Obsolete numeric Anlas budget consulted")

    state.db.month_anlas = state.db.month_anlas_all = obsolete
    with pytest.raises(main.GateError) as caught:
        await main.quota_image_check(key, {"anlas": 1, "v5": 0})
    assert caught.value.status == 402
    assert "仅支持免费" in caught.value.message
    # Admin/service operations are not subject to ordinary feature refusal.
    await main.quota_image_check(key | {"is_admin": True}, {"anlas": 10000, "v5": 0})


@pytest.mark.parametrize("parameters", [
    {"image": "source"}, {"mask": "mask"},
    {"director_reference_images_cached": [{"data": "precise"}]},
    {"characterReferences": [{"data": "character"}]},
    {"controlnet_model": "paid"},
])
def test_clamp_refuses_intrinsically_paid_features_even_when_permitted(parameters):
    _, _, problem = clamp_image_params(image_body(**parameters), max_pixels=1048576,
                                       max_steps=28, allow_img2img=True)
    assert problem and "Anlas" in problem


@pytest.mark.parametrize("cached", [False, True])
def test_excess_encoded_vibes_rejected_without_dropping_aligned_arrays(cached):
    from test_policy_integration import payload
    body = payload(vibes=5)
    p = body["parameters"]
    if cached:
        p["reference_image_multiple_cached"] = [
            {"cache_secret_key": "a" * 64, "data": data}
            for data in p.pop("reference_image_multiple")]
    original = copy.deepcopy(body)
    _, _, problem = clamp_image_params(body, max_pixels=1048576, max_steps=28, allow_img2img=True)
    assert problem and "4" in problem and "Anlas" in problem
    assert body == original


@pytest.mark.asyncio
async def test_v5_exhaustion_never_dispatches_paid_fallback(state):
    state.image_reservations = {}
    async def upstream(_method, _url, _body=None, **options):
        await options["resolve_v5_cost"](True)
        state.nai.calls.append("billable-dispatch")
        raise AssertionError("Paid fallback was dispatched")

    state.nai.request = upstream
    body = image_body()
    body["model"] = "nai-diffusion-5-full"
    response = await post("/ai/generate-image", body)
    assert response.status_code == 402
    assert "V5" in response.json()["error"]["message"]
    assert "Anlas" in response.json()["error"]["message"]
    assert not state.nai.calls and not state.db.charges
    assert not state.image_reservations


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["img2img", "infill", "upscale", "unknown", None, [], {}])
@pytest.mark.parametrize("streaming", [False, True])
async def test_ordinary_paid_actions_refused_without_sources(state, action, streaming):
    body = image_body()
    body["action"] = action
    response = await post("/ai/generate-image" + ("-stream" if streaming else ""), body)
    assert response.status_code == 400
    assert "generate" in response.json()["error"]["message"]
    assert not state.nai.calls and not state.db.charges


@pytest.mark.parametrize("model", ["nai-diffusion-4-5-full", "nai-diffusion-5-full"])
def test_missing_dimensions_are_canonical_free_dimensions(model):
    body = image_body(steps=50)
    body["model"] = model
    del body["parameters"]["width"]
    del body["parameters"]["height"]
    out, _, problem = clamp_image_params(body, max_pixels=1048576, max_steps=100, allow_img2img=True)
    assert problem is None
    assert out["parameters"]["width"] == 832
    assert out["parameters"]["height"] == 1216
    assert out["parameters"]["steps"] <= 28
    assert estimate_image_cost(out)["anlas"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["width", "height", "n_samples"])
@pytest.mark.parametrize("value", [0, -64, True, 1.5, "NaN", None])
async def test_invalid_free_dimensions_or_samples_never_dispatch(state, field, value):
    response = await post("/ai/generate-image", image_body(**{field: value}))
    assert response.status_code == 400
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_me_reports_free_only_and_vibe_entitlement_not_anlas_budgets(state, admin):
    from test_generation_integration import request
    key = state.db.keys["fixture-1"]
    key.update(is_admin=admin, daily_text_tokens=0)

    async def counters(*args):
        return dict(images=2, legacy_free_images=2, anlas=6, v5=0, text_tokens=0, requests=5)

    async def totals(*args):
        return {1: 2}

    async def remaining(key_id, day=None):
        assert (key_id, day) == (1, None)  # Database chooses the Shanghai Vibe day.
        return 0

    state.db.get_counter = counters
    state.db.generated_image_totals = totals
    state.db.get_vibe_remaining = remaining
    data = await main.v1_me(request({}))
    assert data["image_policy"]["free_only"] is (not admin)
    assert data["image_policy"]["anlas_quota_enabled"] is False
    assert data["image_policy"]["max_steps"] == (None if admin else 28)
    assert data["image_policy"]["v5_max_steps"] == (None if admin else 28)
    assert data["today"]["vibe_remaining"] == (None if admin else 0)
    assert data["today"]["vibe_daily_limit"] == (None if admin else 3)
    assert data["today"]["daily_anlas"] == 0
    assert data["today"]["monthly_anlas"] == 0
    assert data["today"]["anlas_today"] == 6  # Audit counters survive.


def test_admin_ui_has_no_obsolete_anlas_permission_or_budget_editors():
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / "app/static"
    html = (static / "index.html").read_text()
    script = (static / "assets/gate.js").read_text()
    for obsolete in ("f_daily_anlas", "f_monthly_anlas", "f_allow_anlas", "s_budget",
                     "global_monthly_anlas:", "default_daily_anlas", "default_monthly_anlas",
                     "default_allow_anlas", "Anlas 预算"):
        assert obsolete not in html + script
    assert "仅支持免费" in html
    assert "/vibe" in html and "3 次" in html and "零点" in html
    assert "High 最多 28 步" in html and "Medium 固定 14 步" in html
    assert "Anlas" in html and "审计" in script  # Usage audit remains visible.
    assert "account.allow_anlas" in script and "'/allow-anlas'" in script
    assert 'id="upstreamNewAnlas"' in html  # Real upstream paid permissions survive.
    assert "与 23 取小" not in script
    assert "min:1,max:28,unit:'步'" in script  # Match local configuration API bounds.
    assert "High 和旧模型均取此值与 28 的较小值" in script


def test_registration_message_explains_vibe_daily_encode_reset():
    from app.registration import RegistrationService
    message = RegistrationService._key_message("nai-fixture")
    assert "/vibe" in message and "3" in message
    assert "零点" in message and "编码" in message
    assert "免费" in message
