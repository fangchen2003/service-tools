"""在网关容器里做一份「不含原图」的一致快照（naigate-snapshot 调用：docker exec -i nai-gate python - OUT < snapshot.py）。

为什么不再用宿主机 sqlite3 .backup：库里 90% 是原图 BLOB（1.3GB），整库复制 + VACUUM 要好几分钟、把磁盘 IO 打满，
网关写库等锁超过 15 秒就报 database is locked（2026-10-10 19:07 / 19:20 两次部署，成员收到几十个 500）。
现在：同一个只读事务里把每张表 INSERT ... SELECT 到新库，generation_audit 不读 image / image_type 两列
（它们在行尾，不选就不会读溢出页），只读几十 MB。WAL 下读事务不挡写。
"""
import sqlite3
import sys

SRC = "/app/data/nai_gate.db"
OUT = sys.argv[1]
SKIP = {"generation_audit": ("image", "image_type", "image_file", "image_size")}   # 原图（BLOB 或文件）不进备份

dst = sqlite3.connect(f"file:{OUT}", uri=True, isolation_level=None)
dst.execute("ATTACH DATABASE ? AS s", (f"file:{SRC}?mode=ro",))
dst.execute("BEGIN")                                   # 一个读事务：所有表是同一时刻的数据
objs = dst.execute("SELECT type, name, sql FROM s.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
                   "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END").fetchall()
for typ, name, sql in objs:
    if typ != "table":
        continue
    dst.execute(sql)
    cols = [r[1] for r in dst.execute(f'PRAGMA s.table_info("{name}")')]
    keep = [c for c in cols if c not in SKIP.get(name, ())]
    cl = ", ".join(f'"{c}"' for c in keep)
    dst.execute(f'INSERT INTO main."{name}" ({cl}) SELECT {cl} FROM s."{name}"')
for typ, name, sql in objs:
    if typ in ("index", "trigger", "view"):
        dst.execute(sql)
seq = dst.execute("SELECT name FROM s.sqlite_master WHERE name='sqlite_sequence'").fetchone()
if seq:
    dst.execute("DELETE FROM main.sqlite_sequence")
    dst.execute("INSERT INTO main.sqlite_sequence SELECT * FROM s.sqlite_sequence")
dst.execute("COMMIT")
dst.execute("DETACH DATABASE s")
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
keys = dst.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
dst.close()
print(ok, keys)
