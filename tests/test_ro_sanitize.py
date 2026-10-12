import sqlite3
import subprocess
import sys
from pathlib import Path


def test_ro_sanitize_strips_secrets(tmp_path):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.executescript("""
    CREATE TABLE api_keys (id INTEGER PRIMARY KEY, token TEXT);
    CREATE TABLE site_settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE generation_audit (id INTEGER PRIMARY KEY, prompt TEXT, negative TEXT, extra TEXT, thumb BLOB);
    CREATE TABLE discord_registrations (discord_id TEXT, avatar TEXT);
    CREATE TABLE req_features (key_id INTEGER, toks TEXT, fp TEXT);
    CREATE TABLE error_events (sig TEXT, detail TEXT);
    INSERT INTO api_keys VALUES (1, 'nai-secret-abc');
    INSERT INTO site_settings VALUES ('key_source_salt','s'),('admin_password_hash','scrypt$x$y'),('guard_hourly','150');
    INSERT INTO generation_audit VALUES (1,'1girl','bad','{}',x'00');
    INSERT INTO discord_registrations VALUES ('1','a');
    INSERT INTO req_features VALUES (1,'09baeab09e','fp1');
    INSERT INTO error_events VALUES ('s','Traceback ... prompt=1girl');
    """)
    c.commit(); c.close()
    script = Path(__file__).parent.parent / "deploy" / "ops" / "ro_sanitize.py"
    subprocess.run([sys.executable, str(script), str(db)], check=True, capture_output=True)
    c = sqlite3.connect(db)
    assert c.execute("SELECT token FROM api_keys").fetchone()[0].startswith("sha256:")
    assert [r[0] for r in c.execute("SELECT key FROM site_settings")] == ["guard_hourly"]
    assert c.execute("SELECT prompt, negative, extra, thumb FROM generation_audit").fetchone() == ("", "", "", None)
    assert c.execute("SELECT toks, fp FROM req_features").fetchone() == ("", "fp1")
    assert c.execute("SELECT detail FROM error_events").fetchone()[0] == ""
