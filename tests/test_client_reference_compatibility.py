"""Contracts taken from st-chatu8 and Aaalice NAI Launcher, with fake upstreams."""
import base64
import copy
import io

import pytest
from PIL import Image


from app.image_compat import normalize_image_references
from app.image_tools import prepare_tool
from app.policy import estimate_image_cost, validate_image_references
from test_generation_integration import state, image_body, post
from test_image_tools import png
from test_image_attachments import post_upload
from test_image_stream_routes import StreamingNai, Frames, PNG
from test_image_stream_formats import frame

UUID = "12345678-1234-4234-8234-123456789abc"


def reference_image(fmt="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "#334455").save(output, format=fmt)
    return base64.b64encode(output.getvalue()).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
async def test_st_chatu8_precise_reference_uuid_and_mobile_jpeg(state, fmt):
    body = image_body(precise=1)
    body["parameters"]["director_reference_images_cached"] = [
        {"cache_secret_key": UUID, "data": reference_image(fmt)}]
    original = copy.deepcopy(body)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200, response.text
    assert len(state.nai.calls) == len(state.db.charges) == 1
    assert state.db.charges[0][1]["anlas"] == 5
    sent = state.nai.calls[0][2]["parameters"]
    ref = sent["director_reference_images_cached"][0]
    assert len(ref["cache_secret_key"]) == 64
    assert base64.b64decode(ref["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    for name in ("director_reference_descriptions", "director_reference_information_extracted",
                 "director_reference_strength_values", "director_reference_secondary_strength_values"):
        assert sent[name] == body["parameters"][name]
    assert body == original


@pytest.mark.asyncio
async def test_launcher_legacy_json_precise_reference_array(state):
    body = image_body(precise=1)
    p = body["parameters"]
    p["director_reference_images"] = [reference_image()]
    del p["director_reference_images_cached"]
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200, response.text
    assert state.db.charges[0][1]["anlas"] == 5
    assert len(state.nai.calls) == 1


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_adaptation_is_content_bound_key_scoped_and_non_mutating(fmt):
    body = image_body(precise=1)
    body["parameters"]["director_reference_images_cached"] = [
        {"cache_secret_key": UUID, "data": reference_image(fmt)}]
    original = copy.deepcopy(body)
    a = normalize_image_references(body, "fixture-user-a")
    b = normalize_image_references(body, "fixture-user-b")
    assert normalize_image_references(body, "fixture-user-a") == a
    assert normalize_image_references(a, "fixture-user-a") == a
    assert body == original and validate_image_references(a) is None
    field = "director_reference_images_cached"
    assert a["parameters"][field][0]["cache_secret_key"] != b["parameters"][field][0]["cache_secret_key"]
    changed = copy.deepcopy(body)
    changed["parameters"][field][0]["data"] = base64.b64encode(png(128, 64)).decode()
    assert normalize_image_references(changed, "fixture-user-a")["parameters"][field] != a["parameters"][field]


def test_official_png_and_64_hex_payload_kept_byte_for_byte():
    body = image_body(precise=1)
    assert normalize_image_references(body, "fixture-user-a") == body


@pytest.mark.asyncio
@pytest.mark.parametrize("count,anlas", [(1, 0), (5, 2)])
async def test_st_chatu8_vibe_uuid_and_omitted_extraction_amount(state, count, anlas):
    body = image_body()
    body["parameters"].update(reference_image_multiple_cached=[
        {"cache_secret_key": UUID, "data": base64.b64encode(b"encoded-vibe-fixture").decode()}
        for _ in range(count)], reference_strength_multiple=[.6] * count)
    response = await post("/ai/generate-image", body)
    assert response.status_code == 200, response.text
    assert state.db.charges[0][1]["anlas"] == anlas
    assert len(state.nai.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,fmt", [("director_reference_images", "PNG"),
                                     ("director_reference_images_cached", "JPEG")])
async def test_multipart_reference_variants_use_the_same_admission(state, field, fmt):
    body = image_body(precise=1)
    p = body["parameters"]
    if field == "director_reference_images":
        del p["director_reference_images_cached"]
        p[field] = ["director_ref_0"]
    else:
        p[field] = [{"cache_secret_key": UUID, "data": "director_ref_0"}]
    image = base64.b64decode(reference_image(fmt))
    response = await post_upload("/ai/generate-image", body, [("director_ref_0", image)])
    assert response.status_code == 200, response.text
    assert len(state.nai.calls) == 1 and state.db.charges[0][1]["anlas"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["anlas", "daily", "monthly", "model"])
async def test_compatibility_does_not_relax_permissions_and_budgets(state, restriction):
    body = image_body(precise=1)
    body["parameters"]["director_reference_images_cached"][0]["cache_secret_key"] = UUID
    key = state.db.keys["fixture-1"]
    if restriction == "anlas":
        key["allow_anlas"] = False
    elif restriction == "daily":
        key["daily_anlas"] = 4
    elif restriction == "monthly":
        key["monthly_anlas"] = 4
    else:
        body["model"] = "nai-diffusion-5-full"
    response = await post("/ai/generate-image", body)
    assert response.status_code in (400, 402)
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["cache-only", "bad-key", "malformed-image", "both-forms", "too-many"])
async def test_malformed_references_still_reject_before_upstream(state, bad):
    body = image_body(precise=1)
    refs = body["parameters"]["director_reference_images_cached"]
    refs[0]["cache_secret_key"] = UUID
    if bad == "cache-only":
        del refs[0]["data"]
    elif bad == "bad-key":
        refs[0]["cache_secret_key"] = "not-a-cache-key"
    elif bad == "malformed-image":
        refs[0]["data"] = base64.b64encode(b"\xff\xd8\xfffake-jpeg").decode()
    elif bad == "both-forms":
        body["parameters"]["director_reference_images"] = [reference_image()]
    else:
        body["parameters"]["director_reference_images_cached"] = refs * 17
    response = await post("/ai/generate-image", body)
    assert response.status_code == 400
    assert not state.nai.calls and not state.db.charges


def test_raw_reference_cost_cannot_be_misclassified_as_free():
    body = image_body(precise=1)
    p = body["parameters"]
    p["director_reference_images"] = [p.pop("director_reference_images_cached")[0]["data"]]
    assert validate_image_references(body) is None
    assert estimate_image_cost(body) == {"anlas": 5, "v5": 0}


def test_converted_request_size_is_bounded():
    body = image_body(precise=1)
    body["parameters"]["director_reference_images_cached"][0]["cache_secret_key"] = UUID
    with pytest.raises(ValueError, match="请求体过大"):
        normalize_image_references(body, "fixture-user", max_bytes=100)


@pytest.mark.parametrize("value", [True, False])
def test_director_trial_flag_preserved(value):
    payload, _ = prepare_tool({"image": reference_image(), "req_type": "lineart",
                              "use_new_shared_trial": value}, "augment-image")
    assert payload["use_new_shared_trial"] is value


@pytest.mark.parametrize("value", ["true", 1, None])
def test_director_trial_flag_must_be_boolean(value):
    with pytest.raises(ValueError, match="布尔值"):
        prepare_tool({"image": reference_image(), "req_type": "lineart",
                      "use_new_shared_trial": value}, "augment-image")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sse", "msgpack"])
async def test_stream_normalizes_uuid_jpeg_before_the_same_billing(state, mode):
    state.nai = StreamingNai()
    state.nai.content_type = "application/x-msgpack" if mode == "msgpack" else "text/event-stream"
    wire = frame({"event_type": "final", "samp_ix": 0, "image": PNG}, mode)
    state.nai.frames = Frames([wire])
    body = image_body(precise=1, stream=mode)
    body["parameters"]["director_reference_images_cached"] = [
        {"cache_secret_key": UUID, "data": reference_image("JPEG")}]
    response = await post("/ai/generate-image-stream", body)
    assert response.status_code == 200 and response.content == wire
    ref = state.nai.calls[0][1]["parameters"]["director_reference_images_cached"][0]
    assert len(ref["cache_secret_key"]) == 64
    assert base64.b64decode(ref["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert state.nai.counts == [1] and state.db.charges[0][1]["anlas"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["nai-diffusion-3", "nai-diffusion-4-5-full"])
async def test_multipart_raw_vibe_arrays_are_resolved_only_in_image_slots(state, model):
    raw = base64.b64decode(reference_image()) if model == "nai-diffusion-3" else b"encoded-vibe-fixture"
    body = image_body(reference_image_multiple=["ref_multiple_0"],
                      reference_information_extracted_multiple=[.7],
                      reference_strength_multiple=[.6])
    body["model"] = model
    response = await post_upload("/ai/generate-image", body, [("ref_multiple_0", raw)])
    assert response.status_code == 200, response.text
    sent = state.nai.calls[0][2]["parameters"]
    assert sent["reference_image_multiple"] == [base64.b64encode(raw).decode()]
    assert state.db.charges[0][1]["anlas"] == 0


@pytest.mark.asyncio
async def test_jpeg_conversion_rejects_excessive_dimensions_before_upstream(state):
    with io.BytesIO() as buffer:
        Image.new("RGB", (2048, 2048), "#334455").save(buffer, format="JPEG")
        data = base64.b64encode(buffer.getvalue()).decode()
    body = image_body(precise=1)
    body["parameters"]["director_reference_images_cached"] = [{"cache_secret_key": UUID, "data": data}]
    response = await post("/ai/generate-image", body)
    assert response.status_code == 400
    assert not state.nai.calls and not state.db.charges
