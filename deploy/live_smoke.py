#!/usr/bin/env python3
"""发布前线上冒烟测试（成员接口）。

用法（Key 从环境变量读，不会打印出来）：
    read -s GATE_KEY && export GATE_KEY        # 粘贴一个“测试用成员 Key”，回车
    .venv/bin/python deploy/live_smoke.py https://gate.davidzhao.top

只用一把测试 Key；图片请求之间会按网关的 15 秒间隔等待，全程约 4~5 分钟。
"""
import json
import os
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://gate.davidzhao.top").rstrip("/")
KEY = os.environ.get("GATE_KEY", "").strip()
if not KEY:
    sys.exit("请先 export GATE_KEY=<测试成员 Key>")

H = {"Authorization": f"Bearer {KEY}"}
GAP = 16  # 网关默认每 Key 图片间隔 15s
results = []


def show(name, r=None, ok=None, note=""):
    if r is not None:
        ct = r.headers.get("content-type", "")
        body = "" if ("zip" in ct or "image" in ct or "octet" in ct) else r.text[:220].replace("\n", " ")
        line = f"[{r.status_code}] {ct[:28]:28} {len(r.content):>8}B {body}"
    else:
        line = ""
    flag = "PASS" if ok else ("FAIL" if ok is False else "INFO")
    results.append((flag, name))
    print(f"{flag:4} {name:44} {line} {note}".rstrip(), flush=True)


def img_payload(model="nai-diffusion-4-5-full", w=832, h=1216, steps=23, n=1, extra=None):
    p = {
        "width": w, "height": h, "steps": steps, "scale": 5, "n_samples": n,
        "sampler": "k_euler_ancestral", "noise_schedule": "karras", "seed": 42,
        "params_version": 3, "qualityToggle": True, "ucPreset": 0,
        "v4_prompt": {"caption": {"base_caption": "1girl, smoke test", "char_captions": []},
                      "use_coords": False, "use_order": True},
        "v4_negative_prompt": {"caption": {"base_caption": "lowres", "char_captions": []}},
    }
    if extra:
        p.update(extra)
    return {"input": "1girl, smoke test", "model": model, "action": "generate", "parameters": p}


c = httpx.Client(base_url=BASE, timeout=180, follow_redirects=False)

# ---- 1. 鉴权 ----
r = c.get("/v1/me")
show("no key -> 401", r, r.status_code == 401)
r = c.get("/v1/me", headers={"Authorization": "Bearer nai-definitely-wrong"})
show("wrong key -> 401", r, r.status_code == 401)
r = c.get("/v1/me", headers=H)
show("v1/me valid key", r, r.status_code == 200)
me = r.json() if r.status_code == 200 else {}
r = c.get("/user/subscription", headers=H)
show("user/subscription (no upstream secret leak)", r,
     r.status_code == 200 and "pst-" not in r.text)

# ---- 2. 基本生图（免费档） ----
t = time.time()
r = c.post("/ai/generate-image", headers=H, json=img_payload())
show("generate-image v4.5 832x1216/23", r, r.status_code == 200 and len(r.content) > 1000,
     f"{time.time()-t:.1f}s")

# ---- 3. 间隔：紧接着再发，网关应排队约 15 秒后再派发（不是直接拒绝） ----
t = time.time()
r = c.post("/ai/generate-image", headers=H, json=img_payload())
waited = time.time() - t
show("immediate 2nd image is paced (~15s)", r, r.status_code == 200 and waited >= 12, f"{waited:.1f}s")
time.sleep(GAP)

# ---- 4. 超免费档参数：应被钳制或拒绝，不能花 Anlas ----
r = c.post("/ai/generate-image", headers=H,
           json=img_payload(w=1536, h=1536, steps=50, n=4))
show("oversize 1536^2/50 steps/n=4 (clamp or 4xx)", r, r.status_code in (200, 400, 402, 403),
     "→ 去后台用量日志确认该条 Anlas=0/备注为免费")
time.sleep(GAP)

# ---- 5. 字段走私：parameters 外的尺寸 / 字符串数字 ----
bad = img_payload()
bad["width"] = 2048
bad["parameters"]["steps"] = "50"
r = c.post("/ai/generate-image", headers=H, json=bad)
show("smuggled top-level width + str steps", r, r.status_code in (200, 400, 422),
     "→ 后台日志应显示 ≤28 step 免费")
time.sleep(GAP)

# ---- 5b. 小数参数：网关按 int() 截断计价但原样转发，看上游是否按 2 张/29 步执行 ----
r = c.post("/ai/generate-image", headers=H,
           json=img_payload(w=832, h=1216, steps=28.99, n=1.99))
nimg = None
if r.status_code == 200 and r.content[:2] == b"PK":
    import io, zipfile
    nimg = len(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
show("float steps=28.99 n_samples=1.99 -> 400", r, r.status_code == 400,
     f"images_returned={nimg} (>1 即存在白嫖/漏计费)")
time.sleep(GAP)
r = c.post("/ai/generate-image", headers={**H, "content-type": "application/json"},
           content=json.dumps(img_payload()).replace('"scale": 5', '"scale": NaN').encode())
show("NaN in body -> 400 (not 500)", r, r.status_code == 400)
time.sleep(GAP)

# ---- 6. 模型范围：V5 对仅 V4.5 的 Key ----
r = c.post("/ai/generate-image", headers=H, json=img_payload(model="nai-diffusion-5-full"))
scope = me.get("image_model_scope") or me.get("key", {}).get("image_model_scope")
show("V5 model with legacy-scope key", r, r.status_code == 403 if scope == "legacy" else None,
     f"(scope={scope})")
r = c.post("/ai/generate-image", headers=H, json=img_payload(model="NAI-Diffusion-5-Full "))
show("V5 model case/space variant", r, r.status_code in (400, 403) if scope == "legacy" else None)
r = c.post("/ai/generate-image", headers=H, json=img_payload(model="unknown-model"))
show("unknown model -> 4xx", r, 400 <= r.status_code < 500)
time.sleep(GAP)

# ---- 7. img2img / 参考图需要权限 ----
r = c.post("/ai/generate-image", headers=H,
           json={**img_payload(), "action": "img2img",
                 "parameters": {**img_payload()["parameters"], "image": "iVBORw0KGgo=", "strength": 0.7}})
show("img2img without permission -> 4xx", r, 400 <= r.status_code < 500)

# ---- 8. 畸形请求 ----
r = c.post("/ai/generate-image", headers={**H, "content-type": "application/json"}, content=b"{not json")
show("malformed JSON -> 4xx (no traceback)", r,
     400 <= r.status_code < 500 and "Traceback" not in r.text)
r = c.post("/ai/generate-image", headers={**H, "content-type": "application/json"},
           content=b'{"input":"' + b"a" * (30 * 1024 * 1024) + b'"}')
show("30MB body -> 413", r, r.status_code == 413)

# ---- 9. 流式：读到首个事件后主动断开，随后应能继续使用 ----
time.sleep(GAP)
t = time.time()
try:
    with c.stream("POST", "/ai/generate-image-stream", headers=H,
                  json={**img_payload(), "parameters": {**img_payload()["parameters"], "stream": "msgpack"}}) as s:
        status = s.status_code
        for _ in s.iter_bytes():
            break
    show("stream: open then abort", None, status == 200, f"status={status} {time.time()-t:.1f}s")
except Exception as e:  # noqa: BLE001
    show("stream: open then abort", None, False, repr(e)[:120])
time.sleep(GAP)
r = c.get("/queue-status")
show("queue-status after abort (active should be 0)", r,
     r.status_code == 200 and r.json().get("global", {}).get("active") == 0)
r = c.post("/ai/generate-image", headers=H, json=img_payload())
show("image after aborted stream still works", r, r.status_code == 200)

# ---- 10. 其它功能（结果取决于该 Key 的功能开关） ----
r = c.get("/ai/generate-image/suggest-tags", headers=H,
          params={"model": "nai-diffusion-4-5-full", "prompt": "1gi"})
show("suggest-tags (403 if feature off)", r, None)
r = c.post("/v1/chat/completions", headers=H,
           json={"model": "llama-3-erato-v1", "max_tokens": 8,
                 "messages": [{"role": "user", "content": "hi"}]})
show("v1/chat/completions (403 if feature off)", r, None)
r = c.post("/ai/generate-voice", headers=H, json={"text": "hi", "voice": -1, "seed": "Aini", "opus": False, "version": "v2"})
show("generate-voice (member) -> 403", r, r.status_code == 403)

# ---- 11. 别名路径同样受控 ----
r = c.post("/nai/ai/generate-image", json=img_payload())
show("alias /nai/ai/generate-image no key -> 401", r, r.status_code == 401)

# ---- 12. 公开面 ----
for path in ("/openapi.json", "/docs", "/self-register/quota", "/self-register/resetkey"):
    r = c.get(path)
    show(f"public GET {path}", r, r.status_code == 404 if path != "/self-register/resetkey" else r.status_code in (404, 405))
r = c.get("/admin")
show("/admin has frame-ancestors/XFO", r,
     "frame-ancestors" in r.headers.get("content-security-policy", "") or "x-frame-options" in r.headers)
r = c.get("/")
show("HSTS on /", r, "strict-transport-security" in r.headers)

r = c.get("/v1/me", headers=H)
show("v1/me after run (check usage counters)", r, r.status_code == 200)

print("\n=== 汇总 ===")
for flag in ("FAIL", "PASS", "INFO"):
    print(flag, sum(1 for f, _ in results if f == flag))
