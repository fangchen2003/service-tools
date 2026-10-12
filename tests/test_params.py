"""私密参数：服务器文件里的值覆盖代码里的公开起点值；文件坏了退回起点；修改即时生效。"""
import json
import os

from app import params
from app.share_guard import decayed


def test_private_values_override_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert params.P("share.warn", 30) == 30 and not params.overridden("share.warn")
    params.update({"share.warn": 45, "share.half_life": 3600})
    assert params.P("share.warn", 30) == 45 and params.overridden("share.warn")
    assert abs(decayed(100, 0, 3600) - 50) < 1e-6                       # 私密半衰期生效
    assert oct(os.stat(params.path()).st_mode & 0o777) == "0o600"
    params.update({"share.warn": None})
    assert params.P("share.warn", 30) == 30
    with open(params.path(), "w") as f:
        f.write("{broken")
    params._MTIME = -1.0
    assert params.P("share.warn", 30) == 30                              # 文件坏了不影响服务
    snap = params.snapshot()
    assert snap["share.warn"]["default"] == 30
