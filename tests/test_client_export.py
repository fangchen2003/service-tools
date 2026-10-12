import csv
import io
import time
import zipfile

import pytest

from app import client_export
from app.database import Database


@pytest.mark.asyncio
async def test_export_zip_has_sorted_requests_with_device_features(tmp_path):
    db = Database(str(tmp_path / "x.sqlite"))
    await db.connect()
    try:
        k = await db.create_key({"name": "成员A", "token": "nai-secret-token-a", "daily_images": 150, "daily_anlas": 0,
                                 "daily_v5": 0, "monthly_anlas": 0, "daily_text_tokens": 0, "rpm": 10,
                                 "allow_anlas": False, "allow_img2img": False, "exclude_global_v5": False,
                                 "image_model_scope": "all"})
        now = time.time()
        await db._db.execute("INSERT INTO req_features(ts,key_id,src,fp,os,sig,toks,busy) VALUES (?,?,?,?,?,?,?,?)",
                             (now - 100, k["id"], "1.2.*.*", "fpA", "Android", "sigA", "", 0))
        for dt, st in ((90, "ok"), (200, "rejected")):
            await db._db.execute("INSERT INTO usage_log (ts,key_id,key_name,kind,status,images,src,client) "
                                 "VALUES (?,?,?,?,?,?,?,?)", (now - dt, k["id"], "成员A", "image", st, 1, "1.2.*.*", "UA"))
        await db._db.commit()
        blob = client_export.build_zip(await client_export.collect(db, 30))
    finally:
        await db.close()
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert set(z.namelist()) == {"README.txt", "requests.csv", "keys.csv", "networks.csv", "share_evidence.csv"}
    rows = list(csv.DictReader(io.StringIO(z.read("requests.csv").decode("utf-8-sig"))))
    assert [r["status"] for r in rows] == ["rejected", "ok"]               # 按时间升序
    assert rows[1]["device_fp"] == "fpA" and rows[0]["device_fp"] == ""     # 只匹配请求之前的特征
    keys = list(csv.DictReader(io.StringIO(z.read("keys.csv").decode("utf-8-sig"))))
    assert keys[0]["devices"] == "1" and keys[0]["ok_images"] == "1"
    assert b"nai-secret-token-a" not in blob


def test_csv_formula_injection_is_neutralised():
    from app.client_export import safe_cell
    assert safe_cell('=HYPERLINK("http://x","a")').startswith("'=")
    assert safe_cell("@SUM(1)") == "'@SUM(1)" and safe_cell("+cmd") == "'+cmd"
    assert safe_cell("Mozilla/5.0") == "Mozilla/5.0" and safe_cell(12) == 12


def test_export_matches_device_by_arrival_time():
    from app.client_export import build_zip
    now = 1_800_000_000.0
    d = {"keys": [(1, "k", 1, 0, 0, 1, now - 999, now)], "reg": {}, "sources": [], "share": {}, "evidence": [], "tags": {}, "days": 1,
         # A 在 t=0 到达、t=40 完成；B 在 t=10 到达
         "feats": [(now, 1, "", "fpA", "Win", "sigA"), (now + 10, 1, "", "fpB", "Win", "sigB")],
         "usage": [(now + 40, 1, "k", "image", "image", "ok", 1, 0, 0, 40000, 200, "", "UA", "", "", "")]}
    z = zipfile.ZipFile(io.BytesIO(build_zip(d)))
    rows = list(csv.DictReader(io.StringIO(z.read("requests.csv").decode("utf-8-sig"))))
    assert rows[0]["device_fp"] == "fpA"
