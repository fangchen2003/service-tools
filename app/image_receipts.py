"""Small opt-in receipts containing dispatched settings and settled Gate usage."""

import json


RECEIPT_HEADER = "x-nai-gate-receipt"
PARAMETERS = ("width", "height", "steps", "sampler", "seed", "scale", "cfg_rescale",
              "strength", "noise", "noise_schedule", "upscaled_enhance", "req_type", "defry")


def image_receipt(body, operation, completed, cost, legacy_free_images=0):
    parameters = body.get("parameters", body)
    return {"version": 1, "source": "gate", "operation": operation,
            "model": body.get("model"),
            "parameters": {name: parameters[name] for name in PARAMETERS
                           if name in parameters and type(parameters[name]) in (str, int, float, bool)},
            "completed_images": completed,
            "cost": {"anlas": cost["anlas"], "v5": cost["v5"],
                     "legacy_free_images": legacy_free_images}}


def receipt_headers(request, receipt):
    if request.headers.get(RECEIPT_HEADER) != "1":
        return {}
    return {RECEIPT_HEADER: json.dumps(receipt, ensure_ascii=True, separators=(",", ":"))}
