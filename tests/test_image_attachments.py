"""Launcher-style uploads must use the same admission and cost paths as JSON."""
import asyncio
import base64
import json

import httpx
import pytest
from starlette.datastructures import UploadFile
from starlette.requests import Request

from app import main
from app.policy import estimate_image_cost
from test_body_limit import chunk_request
from test_generation_integration import state, image_body
from test_image_tools import png, zipped
from test_image_stream_routes import StreamingNai, Frames, PNG
from test_image_stream_formats import frame, unpack_frames


def multipart(payload, files=()):
    # Launcher puts binary image parts first, then a JSON file named request.
    parts = [(name, ("blob", data, "image/png")) for name, data in files]
    parts.append(("request", ("blob", json.dumps(payload).encode(), "application/json")))
    request = httpx.Request("POST", "http://fixture.invalid", files=parts)
    return request.read(), request.headers["content-type"]


async def post_upload(path, payload, files=()):
    raw, content_type = multipart(payload, files)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fixture.invalid") as client:
        return await client.post(path, content=raw, headers={
            "Content-Type": content_type, "Authorization": "Bearer fixture-1"})


async def parse(payload, files=(), limit_mb=25):
    raw, content_type = multipart(payload, files)
    request, _ = chunk_request([raw], content_type=content_type.encode())
    return await main.read_image_payload(request, limit_mb)


@pytest.mark.asyncio
async def test_image_mask_and_reference_dedup_preserve_metadata():
    payload = image_body(precise=2, image="image", mask="mask")
    for ref in payload["parameters"]["director_reference_images_cached"]:
        ref["data"] = "image"  # Identical content reuses the first image part.
    actual = await parse(payload, [("image", png(64, 64)), ("mask", png(64, 64))])
    params = actual["parameters"]
    assert params["image"] == params["mask"] == base64.b64encode(png(64, 64)).decode()
    assert all(ref == {"cache_secret_key": "a" * 64, "data": params["image"]}
               for ref in params["director_reference_images_cached"])
    assert params["director_reference_descriptions"] == payload["parameters"]["director_reference_descriptions"]
    assert actual["input"] == payload["input"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["image", "mask", "vibe", "precise"])
async def test_uploads_reach_existing_generation_validation_and_accounting(state, mode):
    image = png(64, 64)
    payload = image_body()
    params = payload["parameters"]
    files = [("image", image)]
    if mode in ("image", "mask"):
        params["image"] = "image"
        if mode == "mask":
            params["mask"] = "image"
            payload.update(action="infill", model="nai-diffusion-4-5-full-inpainting")
    elif mode == "vibe":
        image = b"fixture-encoded-vibe"
        files = [("ref_multiple_0", image)]
        params.update(reference_image_multiple_cached=[{"cache_secret_key": "a" * 64, "data": "ref_multiple_0"}],
                      reference_strength_multiple=[.6])
    else:
        payload = image_body(precise=1)
        payload["parameters"]["director_reference_images_cached"][0]["data"] = "director_ref_0"
        files = [("director_ref_0", image)]
    response = await post_upload("/ai/generate-image", payload, files)
    assert response.status_code == 200
    upstream = state.nai.calls[0][2]
    assert base64.b64encode(image).decode() in json.dumps(upstream)
    assert state.db.charges[0][1]["anlas"] == estimate_image_cost(upstream)["anlas"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["site", "key", "anlas", "root-image", "v5-reference"])
async def test_upload_format_cannot_skip_admission(state, restriction):
    payload = image_body(image="image")
    if restriction == "site":
        state.settings.allow_img2img = False
    elif restriction == "key":
        state.db.keys["fixture-1"]["allow_img2img"] = False
    elif restriction == "anlas":
        state.db.keys["fixture-1"]["allow_anlas"] = False
    elif restriction == "root-image":
        payload["image"] = payload["parameters"].pop("image")
    else:
        payload = image_body(precise=1)
        payload["model"] = "nai-diffusion-5"
        payload["parameters"]["director_reference_images_cached"][0]["data"] = "image"
    response = await post_upload("/ai/generate-image", payload, [("image", png(64, 64))])
    assert response.status_code in (400, 402, 403)
    assert not state.nai.calls and not state.db.charges


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upscale", "augment-image", "encode-vibe"])
async def test_image_tool_uploads_and_vibe_field_alias(state, operation):
    payload = {"image": "image", "model": "nai-diffusion-5-curated", "declared_blur_sigma": 0}
    state.nai.content = zipped()
    if operation == "augment-image":
        payload = {"image": "image", "width": 1024, "height": 1024, "req_type": "lineart"}
    elif operation == "encode-vibe":
        payload = {"image": "image", "model": "nai-diffusion-4-5-full", "information_extracted": .7}
        state.nai.content = b"fixture-encoded-vibe"
    response = await post_upload("/ai/" + operation, payload, [("image", png())])
    assert response.status_code == 200
    assert state.nai.calls[0][2]["image"] == base64.b64encode(png()).decode()
    if operation == "encode-vibe":
        assert state.nai.calls[0][2]["information_extracted"] == .7
    assert len(state.db.charges) == 1


@pytest.mark.asyncio
async def test_conflicting_vibe_encoding_fields_rejected_before_dispatch(state):
    response = await post_upload("/ai/encode-vibe", {
        "image": "image", "model": "nai-diffusion-4-5-full",
        "informationExtracted": .7, "information_extracted": .8}, [("image", png())])
    assert response.status_code == 400 and not state.nai.calls


@pytest.mark.asyncio
async def test_multipart_generation_with_binary_stream(state):
    state.nai = StreamingNai()
    state.nai.content_type = "application/x-msgpack"
    state.nai.frames = Frames([frame({"event_type": "final", "samp_ix": 0, "image": PNG}, "msgpack")])
    response = await post_upload("/ai/generate-image-stream", image_body(image="image", stream="msgpack"),
                                 [("image", png(64, 64))])
    assert response.status_code == 200 and unpack_frames(response.content)[0]["event_type"] == "final"
    assert state.nai.calls[0][1]["parameters"]["image"] == base64.b64encode(png(64, 64)).decode()
    assert state.nai.counts == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,files", [
    ({"image": "image"}, []),
    ({"image": "image"}, [("image", b"")]),
    ({"image": "image"}, [("image", b"one"), ("image", b"two")]),
    ({"image": "image"}, [("image", b"one"), ("request", b"{}")]),
    ({"input": "image"}, [("image", b"unused")]),
    ({"image": "../private"}, [("../private", b"one")]),
])
async def test_bad_attachments_close_all_files(payload, files, monkeypatch):
    closed = []
    original = UploadFile.close
    async def close(upload):
        await original(upload)
        closed.append(upload.file.closed)
    monkeypatch.setattr(UploadFile, "close", close)
    with pytest.raises(main.GateError) as exc:
        await parse(payload, files)
    assert exc.value.status == 400 and closed == [True] * (len(files) + 1)


@pytest.mark.asyncio
async def test_repeated_pointer_expansion_cannot_bypass_json_size_limit():
    payload, files = {"image": "image", "mask": "image"}, [("image", b"x" * 3072)]
    raw, _ = multipart(payload, files)
    with pytest.raises(main.GateError) as exc:
        await parse(payload, files, len(raw) / (1024 * 1024))
    assert exc.value.status == 413


@pytest.mark.asyncio
async def test_cancel_during_form_parse_waits_for_file_cleanup(monkeypatch):
    original, entered, release, uploads = Request.form, asyncio.Event(), asyncio.Event(), []
    async def form(request, **kwargs):
        result = await original(request, **kwargs)
        uploads.extend(part for _, part in result.multi_items() if isinstance(part, UploadFile))
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(Request, "form", form)
    task = asyncio.create_task(parse({"image": "image"}, [("image", png(64, 64))]))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(uploads) == 2 and all(upload.file.closed for upload in uploads)
