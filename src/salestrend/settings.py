"""运行配置：环境变量 + config/platforms.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(os.getenv("SALESTREND_ROOT", Path(__file__).resolve().parents[2]))


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    ch_host: str = os.getenv("CH_HOST", "localhost")
    ch_port: int = _env_int("CH_PORT", 8123)
    ch_user: str = os.getenv("CH_USER", "salestrend")
    ch_password: str = os.getenv("CH_PASSWORD", "salestrend")
    ch_database: str = os.getenv("CH_DATABASE", "salestrend")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    query_timeout_s: float = float(os.getenv("QUERY_TIMEOUT_S", "2"))
    rate_per_s: float = float(os.getenv("RATE_LIMIT_PER_S", "50"))
    rate_burst: int = _env_int("RATE_LIMIT_BURST", 100)
    max_range_days: int = 366
    rules_dir: Path = ROOT / "rules"
    platforms: dict[str, str] = field(default_factory=dict)
    preset_timezones: tuple[str, ...] = ()


@lru_cache(1)
def get_settings() -> Settings:
    cfg = yaml.safe_load((ROOT / "config" / "platforms.yaml").read_text())
    return Settings(platforms=dict(cfg["platforms"]), preset_timezones=tuple(cfg["preset_timezones"]))
