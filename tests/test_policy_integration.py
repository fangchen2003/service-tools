"""Reference policy regression checks; no upstream or configuration imports."""
import base64
import pytest
from app.policy import estimate_image_cost, opus_free_eligible, validate_image_references, validate_vibe_encoding, VIBE_ENCODING_ANLAS

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nmock-image").decode()
ENCODED = base64.b64encode(b"\x00\x01mock-vibe-vector").decode()


def payload(model="nai-diffusion-4-5-full", *, vibes=0, precise=0, **settings):
    p = {"width": 1024, "height": 1024, "steps": 28, "n_samples": 1,
         "sm": False, "sm_dyn": False}
    if vibes:
        image = PNG if model.startswith(("nai-diffusion-3", "nai-diffusion-furry-3")) else ENCODED
        p.update(reference_image_multiple=[image] * vibes,
                 reference_information_extracted_multiple=[0.7] * vibes,
                 reference_strength_multiple=[0.5] * vibes)
    if precise:
        p.update(
            director_reference_images_cached=[{"cache_secret_key": "a" * 64, "data": PNG} for _ in range(precise)],
            director_reference_descriptions=[{"caption": {"base_caption": "character", "char_captions": []}, "legacy_uc": False} for _ in range(precise)],
            director_reference_information_extracted=[1] * precise,
            director_reference_strength_values=[0.8] * precise,
            director_reference_secondary_strength_values=[0] * precise,
        )
    p.update(settings)
    return {"input": "test", "model": model, "action": "generate", "parameters": p}


@pytest.mark.parametrize(("vibes", "anlas"), [(1, 0), (4, 0), (5, 2), (16, 24)])
def test_vibe_surcharge_preserves_base_opus_discount(vibes, anlas):
    body = payload(vibes=vibes)
    assert estimate_image_cost(body) == {"anlas": anlas, "v5": 0}
    assert opus_free_eligible(body) is (anlas == 0)


def test_precise_is_charged_per_reference_per_output_image():
    assert estimate_image_cost(payload(precise=2)) == {"anlas": 10, "v5": 0}
    assert not opus_free_eligible(payload(precise=1))
    assert estimate_image_cost(payload(precise=2, n_samples=2)) == {"anlas": 60, "v5": 0}
    assert estimate_image_cost(payload(precise=1), is_opus=False) == {"anlas": 25, "v5": 0}


def test_v3_raw_vibes_do_not_acquire_v4_encoding_or_multivibe_charges():
    assert estimate_image_cost(payload("nai-diffusion-3", vibes=16)) == {"anlas": 0, "v5": 0}
    body = payload("nai-diffusion-3", vibes=1)
    body["parameters"]["reference_image_multiple"] = [ENCODED]
    assert validate_image_references(body)
    body = payload(vibes=1)
    body["parameters"]["reference_image_multiple"] = [PNG]
    assert validate_image_references(body)  # V4 input must be encoded explicitly.


@pytest.mark.parametrize("model", ["nai-diffusion-5-full", "nai-diffusion-2", "safe-diffusion"])
def test_unsupported_models_cannot_be_mispriced_as_free(model):
    with pytest.raises(ValueError):
        estimate_image_cost(payload(model, vibes=1))


@pytest.mark.parametrize("model", ["nai-diffusion-4-full", "nai-diffusion-5-full", "nai-diffusion-3"])
def test_precise_only_v45(model):
    assert validate_image_references(payload(model, precise=1))


def test_reference_arrays_data_and_mutual_exclusion_are_validated():
    invalid = [payload(vibes=1, precise=1), payload(vibes=17)]
    missing = payload(vibes=2)
    missing["parameters"]["reference_strength_multiple"] = [0.5]
    invalid.append(missing)
    boolean = payload(precise=1)
    boolean["parameters"]["director_reference_strength_values"] = [True]
    invalid.append(boolean)
    cached_only = payload(precise=1)
    del cached_only["parameters"]["director_reference_images_cached"][0]["data"]
    invalid.append(cached_only)
    bad_key = payload(precise=1)
    bad_key["parameters"]["director_reference_images_cached"][0]["cache_secret_key"] = "uuid-not-cache-key"
    invalid.append(bad_key)
    for body in invalid:
        assert validate_image_references(body)


def test_cached_vibe_form_cannot_bypass_surcharge():
    body = payload(vibes=5)
    p = body["parameters"]
    p["reference_image_multiple_cached"] = [{"cache_secret_key": "b" * 64, "data": item} for item in p.pop("reference_image_multiple")]
    assert validate_image_references(body) is None
    assert estimate_image_cost(body)["anlas"] == 2



def test_vibe_encoding_validation_and_price():
    body = {"model": "nai-diffusion-4-5-full", "image": PNG, "informationExtracted": 0.7}
    assert VIBE_ENCODING_ANLAS == 2
    assert validate_vibe_encoding(body) is None
    for patch in ({"model": "nai-diffusion-5"}, {"image": "not base64"}, {"informationExtracted": float("nan")}, {"informationExtracted": True}):
        assert validate_vibe_encoding({**body, **patch})
