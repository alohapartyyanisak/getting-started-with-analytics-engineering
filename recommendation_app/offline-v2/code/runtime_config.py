from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    load_dotenv = None


_ENV_LOADED = False


def _load_env_once() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if load_dotenv is not None:
        load_dotenv()
    _ENV_LOADED = True


def _as_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "on"}


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return int(default)


def _as_path(value: str, default: Path) -> Path:
    text = str(value or "").strip()
    return Path(text).expanduser() if text else default.expanduser()


_load_env_once()

CODE_DIR = Path(__file__).resolve().parent
OFFLINE_V2_DIR = CODE_DIR.parent

BASELINE_DATASET_ID = os.getenv("DJ_BASELINE_DATASET_ID", "salvatorerastelli/spotify-and-youtube").strip()
EXPANSION_DATASET_ID = os.getenv("DJ_EXPANSION_DATASET_ID", "solomonameh/spotify-music-dataset").strip()
MERGE_CACHE_DIR = _as_path(
    os.getenv("DJ_MERGE_CACHE_DIR", ""),
    OFFLINE_V2_DIR / ".cache" / "merge_cache",
)
YT_RESOLVER_CACHE_PATH = _as_path(
    os.getenv("YT_RESOLVER_CACHE_PATH", ""),
    Path("~/.cache/dj_mixing_station/youtube_resolver_cache.sqlite3"),
)
APP_ENV = os.getenv("APP_ENV", "offline").strip().lower()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
DEBUG = _as_bool(os.getenv("DEBUG", ""), default=False)


def is_merge_cache_disabled() -> bool:
    return _as_bool(os.getenv("DJ_DISABLE_MERGE_CACHE", ""), default=False)


def youtube_api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def youtube_api_run_budget_units() -> int:
    return max(0, _as_int(os.getenv("YOUTUBE_API_RUN_BUDGET_UNITS", ""), default=0))


def youtube_api_units_used_session() -> int:
    return max(0, _as_int(os.getenv("YOUTUBE_API_UNITS_USED_SESSION", "0"), default=0))


def set_youtube_api_units_used_session(value: int) -> None:
    os.environ["YOUTUBE_API_UNITS_USED_SESSION"] = str(max(0, int(value)))


def set_youtube_api_run_budget_units(value: int) -> None:
    os.environ["YOUTUBE_API_RUN_BUDGET_UNITS"] = str(max(0, int(value)))

