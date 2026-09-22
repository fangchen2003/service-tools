"""策略层单元测试：Anlas 估算、Opus 免费档判定、参数钳制。"""

from app.policy import (
    clamp_image_params,
    clamp_text_params,
    estimate_image_anlas,
    estimate_image_cost,
    estimate_tokens,
    fit_size,
    image_model_tier,
    legacy_normal_free_eligible,
    opus_free_eligible,
    text_model_host,
)


def img_payload(w=832, h=1216, steps=28, n=1, model=None, **extra):
    payload = {"input": "1girl", "model": model or "nai-diffusion-3", "action": "generate",
               "parameters": {"width": w, "height": h, "steps": steps, "n_samples": n,
                              "sampler": "k_euler_ancestral", "sm": False, "sm_dyn": False}}
    payload["parameters"].update(extra)
    return payload


# ---------------------------------------------------------- 免费档判定 ----

def test_opus_free_basic():
    assert opus_free_eligible(img_payload())


def test_opus_free_max_size_1024():
    assert opus_free_eligible(img_payload(w=1024, h=1024))


def test_not_free_multi_sample():
    assert not opus_free_eligible(img_payload(n=4))


def test_not_free_big_size():
    assert not opus_free_eligible(img_payload(w=1216, h=1856))


def test_not_free_high_steps():
    assert not opus_free_eligible(img_payload(steps=50))


def test_not_free_smea():
    assert not opus_free_eligible(img_payload(sm=True))


def test_not_free_img2img():
    assert not opus_free_eligible(img_payload(image="AAAA"))


# ------------------------------------------------------------ 计费估算 ----

def test_free_single_costs_zero():
    est = estimate_image_cost(img_payload())
    assert est == {"anlas": 0, "v5": 0}


def test_second_image_costs():
    # Opus：第二张起按单张价格计费（首张免费）
    cost = estimate_image_cost(img_payload(n=2))
    single = estimate_image_cost(img_payload())
    assert cost["anlas"] > 0
    assert single["anlas"] == 0


def test_1024_28steps_price_matches_community():
    # V3+ 公式：1024x1024 / 28 steps 单张 20 Anlas（社区实测值）
    payload = img_payload(w=1024, h=1024, steps=28, n=2, sm=False)
    cost = estimate_image_cost(payload)
    assert 15 <= cost["anlas"] / 2 <= 25


# ---------------------------------------------------------------- V5 ----

def test_v5_shaped_uses_allowance():
    """用户实测：三个 Normal 预设走周额度、不扣 Anlas。"""
    for w, h in ((832, 1216), (1216, 832), (1024, 1024)):
        payload = img_payload(model="nai-diffusion-5", w=w, h=h)
        assert estimate_image_cost(payload) == {"anlas": 0, "v5": 1}, (w, h)


def test_v5_small_sizes_also_allowance():
    """逐维小于等于任一预设（如 Small 512x768）同样按额度内处理。"""
    for w, h in ((512, 768), (768, 512), (640, 640)):
        payload = img_payload(model="nai-diffusion-5", w=w, h=h)
        assert estimate_image_cost(payload) == {"anlas": 0, "v5": 1}, (w, h)


def test_v5_nonpreset_inbetween_size_charged():
    """896x1152 面积达标但不是预设（逐维比较不满足）-> 保守按 Anlas 计费。"""
    payload = img_payload(model="nai-diffusion-5", w=896, h=1152)
    est = estimate_image_cost(payload)
    assert est["v5"] == 0 and est["anlas"] > 0


def test_v5_clamp_snaps_to_preset():
    """免费 Key 的 V5 超尺寸请求吸附到最近预设（老模型仍是等比缩小）。"""
    out, notes, err = clamp_image_params(
        img_payload(model="nai-diffusion-5", w=1216, h=1856, steps=28, n=1),
        max_pixels=1048576, max_steps=28, allow_img2img=False)
    assert err is None
    assert (out["parameters"]["width"], out["parameters"]["height"]) == (832, 1216)

    out, _, _ = clamp_image_params(
        img_payload(model="nai-diffusion-5", w=1536, h=1024),
        max_pixels=1048576, max_steps=28, allow_img2img=False)
    assert (out["parameters"]["width"], out["parameters"]["height"]) == (1216, 832)


def test_old_model_custom_sizes_within_pixel_limit_are_free():
    """官方 V4.5 896x1152 / 28 步单张实测 0 Anlas，非预设也免费。"""
    for w, h in ((832, 1216), (1216, 832), (1024, 1024),
                 (896, 1152), (1152, 896), (960, 1024), (512, 768), (2048, 512)):
        payload = img_payload(model="nai-diffusion-4-5-full", w=w, h=h)
        assert legacy_normal_free_eligible(payload)
        assert estimate_image_cost(payload) == {"anlas": 0, "v5": 0}
        out, notes, error = clamp_image_params(
            payload, max_pixels=1048576, max_steps=28, allow_img2img=False)
        assert error is None and not notes
        assert out == payload

    for overrides in (dict(w=1088, h=1024), dict(steps=29), dict(n=2),
                      dict(image="AAAA"), dict(sm=True)):
        payload = img_payload(model="nai-diffusion-4-5-full", **overrides)
        assert not legacy_normal_free_eligible(payload)
        assert estimate_image_cost(payload)["anlas"] > 0


def test_v5_unshaped_burns_anlas_with_multiplier():
    """V5 多张/大图/超步数 -> 按 Anlas 计费（x1.3）。1024²x28 单张 20*1.3=26A。"""
    payload = img_payload(model="nai-diffusion-5", w=1024, h=1024, n=2)
    est = estimate_image_cost(payload)
    assert est["v5"] == 0
    assert 24 <= est["anlas"] / 2 <= 28  # ≈26

    payload = img_payload(model="nai-diffusion-5", w=1216, h=1856, steps=50)
    assert estimate_image_cost(payload)["anlas"] > 80  # 大图高步数很贵(估算≈94A)


def test_v5_model_detection():
    from app.policy import is_v5_model
    assert is_v5_model("nai-diffusion-5")
    assert is_v5_model("nai-diffusion-5-curated")
    assert not is_v5_model("nai-diffusion-4-5-full")
    assert not is_v5_model("nai-diffusion-3")


def test_image_model_tier_is_an_explicit_allowlist():
    assert image_model_tier("nai-diffusion-4-5-full") == "legacy"
    assert image_model_tier("nai-diffusion-3") == "legacy"
    assert image_model_tier("nai-diffusion-5-curated") == "v5"
    assert image_model_tier("nai-diffusion-furry-3") == "legacy"
    assert image_model_tier("safe-diffusion") == "legacy"
    assert image_model_tier("nai-diffusion-6") is None
    assert image_model_tier("made-up-model") is None


def test_img2img_charged():
    payload = img_payload(image="AAAA", strength=0.7)
    assert estimate_image_anlas(payload) > 0


# ---------------------------------------------------------------- 钳制 ----

def test_clamp_forces_single_and_steps():
    out, notes, err = clamp_image_params(
        img_payload(n=4, steps=50), max_pixels=1048576, max_steps=28, allow_img2img=False)
    assert err is None
    assert out["parameters"]["n_samples"] == 1
    assert out["parameters"]["steps"] == 28
    assert notes  # 有变更说明


def test_clamp_shrinks_resolution():
    out, notes, err = clamp_image_params(
        img_payload(w=1216, h=1856), max_pixels=1048576, max_steps=28, allow_img2img=False)
    assert err is None
    p = out["parameters"]
    assert p["width"] * p["height"] <= 1048576
    assert p["width"] % 64 == 0 and p["height"] % 64 == 0


def test_clamp_rejects_img2img_when_disallowed():
    _, _, err = clamp_image_params(
        img_payload(image="AAAA"), max_pixels=1048576, max_steps=28, allow_img2img=False)
    assert err and "img2img" in err


def test_clamp_allows_img2img_when_allowed():
    out, _, err = clamp_image_params(
        img_payload(image="AAAA"), max_pixels=1048576, max_steps=28, allow_img2img=True)
    assert err is None
    assert out["parameters"]["image"] == "AAAA"


def test_fit_size_rounds_to_64():
    w, h = fit_size(1216, 1856, 1048576)
    assert w % 64 == 0 and h % 64 == 0 and w * h <= 1048576
    assert fit_size(832, 1216, 1048576) == (832, 1216)  # 不超限不缩放


# ---------------------------------------------------------------- 文本 ----

def test_clamp_text():
    payload = {"input": "hi" * 10, "model": "kayra-v1",
               "parameters": {"max_length": 999, "min_length": 999}}
    out, notes, err = clamp_text_params(payload, max_output_tokens=300, max_input_chars=1000)
    assert err is None
    assert out["parameters"]["max_length"] == 300
    assert out["parameters"]["min_length"] <= 300


def test_clamp_text_rejects_long_input():
    payload = {"input": "x" * 5000, "parameters": {"max_length": 100}}
    _, _, err = clamp_text_params(payload, max_output_tokens=300, max_input_chars=1000)
    assert err


def test_token_estimate():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 35) == 10
    assert estimate_tokens("你好" * 10) >= 5


def test_text_host_routing():
    assert "text.novelai" in text_model_host("kayra-v1", "https://text.novelai.net", "x")
    assert "text.novelai" in text_model_host("llama-3-erato-v1", "https://text.novelai.net", "x")
    assert "api.novelai" in text_model_host("clio-v1", "https://text.novelai.net",
                                            "https://api.novelai.net")
