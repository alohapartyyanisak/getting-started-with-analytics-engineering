from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
import re
from urllib.parse import parse_qs, urlparse

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, validate_prepared_dataset

if str(cfg.OFFLINE_V2_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.OFFLINE_V2_CODE_DIR))
OFFLINE_V2_MODE_DIR = cfg.OFFLINE_V2_CODE_DIR / "dev"
if str(OFFLINE_V2_MODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFLINE_V2_MODE_DIR))

import application_v2 as app_v2  # type: ignore  # noqa: E402


base_app = app_v2.base_app


@base_app.st.cache_data(show_spinner=False)
def load_phase2_dataset_cached(version: str, dataset_root: str) -> tuple[pd.DataFrame, str]:
    frame, release = load_prepared_dataset(version=version or None, dataset_root=Path(dataset_root))
    return frame, release.version


def read_source_phase2(_uploaded_file: Any, _dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    dataset_root = cfg.dataset_root()
    prepared_df, release_version = load_phase2_dataset_cached(
        version=cfg.DATASET_VERSION or "",
        dataset_root=str(dataset_root),
    )
    source_path = f"managed_snapshot::{release_version}"
    dataset_path = str((dataset_root / "snapshots" / release_version).resolve())
    return prepared_df, source_path, dataset_path


def prepare_music_data_cached_phase2(raw_df: pd.DataFrame) -> pd.DataFrame:
    # Phase 2 runtime reads precomputed prepared dataset artifacts.
    try:
        return validate_prepared_dataset(raw_df.copy())
    except Exception:
        # Safety fallback: if a raw frame is loaded by mistake, keep app operable.
        return app_v2.prepare_music_data_cached_v2(raw_df)


def extract_youtube_video_id_phase2(url: object) -> str | None:
    text = str(url or "").strip()
    if not text:
        return None

    def _clean_video_id(value: object) -> str | None:
        candidate = str(value or "").strip()
        candidate = candidate.split("?", 1)[0].split("&", 1)[0].split("#", 1)[0]
        return candidate if re.fullmatch(r"[A-Za-z0-9_-]{6,15}", candidate) else None

    parsed = urlparse(text)
    if not parsed.netloc and parsed.path:
        parsed = urlparse(f"https://{text}")

    host = parsed.netloc.lower().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "youtu.be" and path_parts:
        video_id = _clean_video_id(path_parts[0])
        if video_id:
            return video_id
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}:
        if parsed.path == "/watch":
            video_id = _clean_video_id(parse_qs(parsed.query).get("v", [None])[0])
            if video_id:
                return video_id
        if path_parts and path_parts[0] in {"shorts", "embed", "live", "v"} and len(path_parts) > 1:
            video_id = _clean_video_id(path_parts[1])
            if video_id:
                return video_id

    for pattern in (
        r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:embed|shorts|live|v)/)([A-Za-z0-9_-]{6,15})",
        r"youtube\.com/watch/([A-Za-z0-9_-]{6,15})",
        r"[?&]v=([A-Za-z0-9_-]{6,15})",
    ):
        match = re.search(pattern, text)
        if match:
            video_id = _clean_video_id(match.group(1))
            if video_id:
                return video_id

    if "/" not in text and "?" not in text and "&" not in text:
        return _clean_video_id(text)
    return None


def normalize_youtube_watch_url_phase2(url: object) -> str | None:
    video_id = extract_youtube_video_id_phase2(url)
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def prefer_direct_youtube_url_phase2(youtube_link: object, fallback_url: object) -> str:
    direct_from_link = normalize_youtube_watch_url_phase2(youtube_link)
    if direct_from_link:
        return direct_from_link
    direct_from_fallback = normalize_youtube_watch_url_phase2(fallback_url)
    if direct_from_fallback:
        return direct_from_fallback
    return str(youtube_link or fallback_url or "").strip()


def resolve_playback_youtube_url_phase2(row: pd.Series) -> str:
    direct_watch = normalize_youtube_watch_url_phase2(row.get("youtube_link")) or normalize_youtube_watch_url_phase2(
        row.get("url_youtube")
    )
    weak_direct = base_app._should_try_live_youtube_override(row, direct_watch)
    search_fallback = base_app.build_youtube_search_url(
        row.get("artist", ""),
        row.get("track", ""),
        row.get("artist_credits", ""),
    )
    if weak_direct:
        live_watch, _, live_score = base_app.resolve_live_youtube_watch_url(
            str(row.get("artist", "")),
            str(row.get("track", "")),
            str(row.get("artist_credits", "")),
        )
        if live_watch and (not direct_watch or live_score >= 8.0):
            return live_watch
    if direct_watch:
        return direct_watch
    fallback = str(row.get("youtube_link") or row.get("url_youtube") or "").strip()
    return fallback or search_fallback


def resolve_playback_youtube_targets_phase2(
    row: pd.Series,
    force_live: bool = False,
    live_timeout: int = 8,
) -> tuple[str, str]:
    direct_watch = normalize_youtube_watch_url_phase2(row.get("youtube_link")) or normalize_youtube_watch_url_phase2(
        row.get("url_youtube")
    )
    weak_direct = base_app._should_try_live_youtube_override(row, direct_watch)
    search_fallback = base_app.build_youtube_search_url(
        row.get("artist", ""),
        row.get("track", ""),
        row.get("artist_credits", ""),
    )

    if force_live or weak_direct:
        live_watch, _, live_score = base_app.resolve_live_youtube_watch_url(
            str(row.get("artist", "")),
            str(row.get("track", "")),
            str(row.get("artist_credits", "")),
            timeout=live_timeout,
        )
        if live_watch and (force_live or live_score >= 8.0):
            return live_watch, live_watch

    if direct_watch:
        return direct_watch, direct_watch

    fallback = str(row.get("youtube_link") or row.get("url_youtube") or "").strip()
    if fallback:
        return fallback, ""
    return search_fallback, ""


def patch_base_app_for_phase2() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2
    base_app.extract_youtube_video_id = extract_youtube_video_id_phase2
    base_app.normalize_youtube_watch_url = normalize_youtube_watch_url_phase2
    base_app.prefer_direct_youtube_url = prefer_direct_youtube_url_phase2
    base_app.resolve_playback_youtube_url = resolve_playback_youtube_url_phase2
    base_app.resolve_playback_youtube_targets = resolve_playback_youtube_targets_phase2


if __name__ == "__main__":
    patch_base_app_for_phase2()
    base_app.main()
