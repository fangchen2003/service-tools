"""全局配置初始值；可管理项在启动后加载后台保存的覆盖值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "y")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    # ---- 基础 ----
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: Path = Path(os.environ.get("DATA_DIR", "data"))
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")
    admin_cookie_secure: bool = field(default_factory=lambda:
        os.environ.get("ADMIN_COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no", "off"})
    secret_key: str = os.environ.get("SECRET_KEY", "")  # 留空则自动生成并持久化
    tz: str = os.environ.get("TZ", "Asia/Shanghai")  # 配额按天的时区

    # ---- 上游 NovelAI ----
    # 支持多个 token（英文逗号分隔），组成令牌池轮询；强烈建议用公益站专用小号
    nai_tokens: list[str] = field(default_factory=list)
    # 以下两个列表与 NAI_TOKENS 一一对应；缺省项保持“不限 V5 / 允许 Anlas”。
    # V5 日限额为 0 表示不限；用于给特定上游账号设置独立预算。
    nai_token_v5_daily_limits: list[int] = field(default_factory=list)
    nai_token_allow_anlas: list[bool] = field(default_factory=list)
    image_host: str = os.environ.get("NAI_IMAGE_HOST", "https://image.novelai.net")
    text_host: str = os.environ.get("NAI_TEXT_HOST", "https://text.novelai.net")
    text_host_legacy: str = os.environ.get(
        "NAI_TEXT_HOST_LEGACY", "https://api.novelai.net"
    )  # 老模型(Sigurd/Clio等)走这里

    # ---- 并发 / 限流 ----
    global_concurrency: int = _int("GLOBAL_CONCURRENCY", 1)  # 文本等非图片请求的全站并发
    key_concurrency: int = _int("KEY_CONCURRENCY", 1)  # 每把虚拟 key 同时可进行的请求数
    queue_timeout: int = _int("QUEUE_TIMEOUT", 90)  # 排队等待的最长时间(秒)；面板可覆盖
    key_image_min_interval: float = _float("KEY_IMAGE_MIN_INTERVAL", 15)
    image_min_interval: float = _float("IMAGE_MIN_INTERVAL", 15)  # 每把上游 Token 的图片请求间隔
    image_429_cooldown_seconds: float = _float("IMAGE_429_COOLDOWN_SECONDS", 60)
    login_max_attempts: int = _int("LOGIN_MAX_ATTEMPTS", 5)
    login_window_seconds: int = _int("LOGIN_WINDOW_SECONDS", 300)
    key_inactivity_delete_days: int = _int("KEY_INACTIVITY_DELETE_DAYS", 3)

    # ---- 普通用户始终强制免费档，safe_clamp 仅保留旧配置兼容 ----
    safe_clamp: bool = _bool("SAFE_CLAMP", True)
    max_pixels: int = _int("MAX_PIXELS", 1024 * 1024)
    max_steps: int = min(28, max(1, _int("MAX_STEPS", 28)))
    allow_img2img: bool = _bool("ALLOW_IMG2IMG", False)  # img2img/局部重绘会消耗 Anlas

    # 旧 Anlas 数值预算仅保留配置兼容，不参与请求准入；用量审计仍保留。
    global_monthly_anlas: float = _float("GLOBAL_MONTHLY_ANLAS", 10000)
    # 全站每日 V5 张数（镜像 NovelAI 服务端 V5 周额度 ~190 张/天的恢复速率）
    global_daily_v5: int = _int("GLOBAL_DAILY_V5", 150)

    # ---- 新 key 的默认配额（管理员可在后台逐 key 修改）----
    default_daily_images: int = _int("DEFAULT_DAILY_IMAGES", 100)  # 每 Key 免费旧模型日限；0=不限
    default_daily_anlas: float = _float("DEFAULT_DAILY_ANLAS", 100)  # 旧兼容字段，不执行限额
    default_daily_v5: int = _int("DEFAULT_DAILY_V5", 50)
    default_monthly_anlas: float = _float("DEFAULT_MONTHLY_ANLAS", 2500)  # 旧兼容字段，不执行限额
    default_daily_text_tokens: int = _int("DEFAULT_DAILY_TEXT_TOKENS", 150000)
    default_rpm: int = _int("DEFAULT_RPM", 10)
    default_expires_days: int = _int("DEFAULT_KEY_EXPIRES_DAYS", 30)
    default_image_model_scope: str = (
        "all" if os.environ.get("DEFAULT_IMAGE_MODEL_SCOPE") == "all" else "legacy"
    )
    default_allow_anlas: bool = _bool("DEFAULT_ALLOW_ANLAS", False)
    default_allow_img2img: bool = _bool("DEFAULT_ALLOW_IMG2IMG", False)
    default_exclude_global_v5: bool = _bool("DEFAULT_EXCLUDE_GLOBAL_V5", False)

    # ---- 文本 ----
    max_text_output_tokens: int = _int("MAX_TEXT_OUTPUT_TOKENS", 300)
    max_input_chars: int = _int("MAX_INPUT_CHARS", 24000)

    # ---- 其他 ----
    seed_demo_key: bool = _bool("SEED_DEMO_KEY", False)
    cors_origins: list[str] = field(default_factory=lambda: [
        origin.strip() for origin in os.environ.get("CORS_ORIGINS", "*").split(",")
        if origin.strip()
    ])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nai_gate.db"

    @property
    def announcement_path(self) -> Path:
        return self.data_dir / "announcement.html"



def load_settings() -> Settings:
    s = Settings()
    raw = os.environ.get("NAI_TOKENS", "") or os.environ.get("NAI_TOKEN", "")
    s.nai_tokens = [t.strip() for t in raw.replace("\n", ",").split(",") if t.strip()]

    def per_token_ints(name: str, default: int) -> list[int]:
        values: list[int] = []
        for item in os.environ.get(name, "").split(","):
            try:
                values.append(max(0, int(item.strip())))
            except (TypeError, ValueError):
                values.append(default)
        return (values + [default] * len(s.nai_tokens))[:len(s.nai_tokens)]

    def per_token_bools(name: str, default: bool) -> list[bool]:
        values = [
            item.strip().lower() in ("1", "true", "yes", "on", "y")
            if item.strip() else default
            for item in os.environ.get(name, "").split(",")
        ]
        return (values + [default] * len(s.nai_tokens))[:len(s.nai_tokens)]

    s.nai_token_v5_daily_limits = per_token_ints("NAI_TOKEN_V5_DAILY_LIMITS", 0)
    s.nai_token_allow_anlas = per_token_bools("NAI_TOKEN_ALLOW_ANLAS", True)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
