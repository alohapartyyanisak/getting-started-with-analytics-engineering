from __future__ import annotations

import asyncio
import json
import re
import time
import warnings
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

# Patch Streamlit cache task creation to avoid creating an un-awaited coroutine
# when no event loop is running (Python 3.12 warning spam).
def _patch_streamlit_timed_cleanup_cache() -> None:
    try:
        from cachetools import TTLCache
        from streamlit import util as _st_util
    except Exception:
        return

    cache_cls = getattr(_st_util, "TimedCleanupCache", None)
    expire_cache_fn = getattr(_st_util, "expire_cache", None)
    if cache_cls is None or expire_cache_fn is None:
        return
    if getattr(cache_cls, "_djms_asyncio_patch_applied", False):
        return

    def _safe_setitem(self: Any, key: Any, value: Any) -> None:
        if getattr(self, "_task", None) is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._task = loop.create_task(expire_cache_fn(self))
        TTLCache.__setitem__(self, key, value)

    cache_cls.__setitem__ = _safe_setitem
    setattr(cache_cls, "_djms_asyncio_patch_applied", True)


_patch_streamlit_timed_cleanup_cache()

# Suppress noisy Streamlit internal cache warning observed on Python 3.12 runtime.
warnings.filterwarnings(
    "ignore",
    category=RuntimeWarning,
    message=r"coroutine 'expire_cache' was never awaited",
)

try:
    from recommendation_app.youtube_live_resolver import resolve_best_youtube_live
except ModuleNotFoundError:
    try:
        from youtube_live_resolver import resolve_best_youtube_live  # type: ignore
    except ModuleNotFoundError:
        resolve_best_youtube_live = None  # type: ignore

try:
    from recommendation_app.recommender_v2_core import (
        MOOD_ADJUSTMENTS,
        build_duration_playlist,
        format_compact_number,
        prepare_music_data,
        recommend_tracks,
    )
except ModuleNotFoundError:
    from recommender_v2_core import (
        MOOD_ADJUSTMENTS,
        build_duration_playlist,
        format_compact_number,
        prepare_music_data,
        recommend_tracks,
    )


DEFAULT_DATASET_ID = "salvatorerastelli/spotify-and-youtube"
PLAYLIST_TOLERANCE_MINUTES = 3
MAX_SEED_SELECTIONS = 5
PERSONALITY_PROFILES = {
    "Party Starter": {
        "description": "High-energy, dance-forward tracks for an outgoing vibe.",
        "targets": {
            "danceability": 0.82,
            "energy": 0.86,
            "valence": 0.72,
            "acousticness": 0.20,
            "tempo_scaled": 0.78,
            "popularity_norm": 0.75,
        },
    },
    "Late Night Chill": {
        "description": "Smooth, lower-intensity tracks for relaxed listening.",
        "targets": {
            "danceability": 0.55,
            "energy": 0.35,
            "valence": 0.45,
            "acousticness": 0.70,
            "tempo_scaled": 0.35,
            "popularity_norm": 0.50,
        },
    },
    "Focus Flow": {
        "description": "Steady, less-vocal-friendly atmosphere for concentration.",
        "targets": {
            "danceability": 0.50,
            "energy": 0.48,
            "valence": 0.50,
            "acousticness": 0.42,
            "instrumentalness": 0.45,
            "tempo_scaled": 0.50,
            "popularity_norm": 0.45,
        },
    },
    "Uplift Me": {
        "description": "Positive mood, melodic energy, and feel-good momentum.",
        "targets": {
            "danceability": 0.68,
            "energy": 0.70,
            "valence": 0.86,
            "acousticness": 0.32,
            "tempo_scaled": 0.62,
            "popularity_norm": 0.62,
        },
    },
    "Underground Explorer": {
        "description": "Lower-popularity picks with strong character and momentum.",
        "targets": {
            "danceability": 0.58,
            "energy": 0.58,
            "valence": 0.45,
            "acousticness": 0.46,
            "tempo_scaled": 0.52,
            "popularity_norm": 0.20,
        },
    },
}
QUICK_VIBE_DEFAULTS = {
    "Party Starter": {"mood": "High Energy", "spotify_weight_pct": 55, "discovery_hits_pct": 85},
    "Late Night Chill": {"mood": "Chill", "spotify_weight_pct": 60, "discovery_hits_pct": 45},
    "Focus Flow": {"mood": "Balanced", "spotify_weight_pct": 50, "discovery_hits_pct": 55},
    "Uplift Me": {"mood": "Uplifting", "spotify_weight_pct": 55, "discovery_hits_pct": 70},
    "Underground Explorer": {"mood": "Dark", "spotify_weight_pct": 50, "discovery_hits_pct": 20},
}

# Curated row-level YouTube overrides for known high-confidence exceptions.
# These are intentionally narrow and keyed by canonical song identity so the
# app can pin a specific watch URL when the published snapshot URL is not the
# desired user-facing playback target.
CURATED_YOUTUBE_WATCH_OVERRIDES: dict[str, str] = {
    "sp:7ef4DlsgrMEH11cDZd32M6": "https://www.youtube.com/watch?v=DkeiKbqa02g",
}


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
          --bg: #070707;
          --panel: #111111;
          --panel-2: #171717;
          --gold: #d4af37;
          --gold-soft: #f1d98a;
          --text: #f5f5f5;
          --muted: #b8b8b8;
          --border: rgba(212, 175, 55, 0.35);
        }
        .stApp {
          background:
            radial-gradient(1300px 600px at 100% -20%, rgba(212,175,55,0.20), transparent 70%),
            radial-gradient(1000px 500px at -20% 120%, rgba(212,175,55,0.10), transparent 65%),
            var(--bg);
          color: var(--text);
        }
        h1, h2, h3, h4, p, span, div, label {
          color: var(--text) !important;
        }
        .hero {
          border: 1px solid var(--border);
          border-radius: 18px;
          padding: 22px;
          background: linear-gradient(160deg, rgba(212,175,55,0.15), rgba(0,0,0,0.55));
          box-shadow: 0 14px 30px rgba(0,0,0,0.45);
          margin-bottom: 18px;
        }
        .hero h1 {
          margin: 0;
          font-size: 2rem;
          letter-spacing: 0.3px;
        }
        .hero p {
          margin: 8px 0 0 0;
          color: var(--muted) !important;
        }
        .metric-card {
          border: 1px solid var(--border);
          border-radius: 14px;
          background: linear-gradient(150deg, rgba(212,175,55,0.12), rgba(17,17,17,0.95));
          padding: 10px 12px;
          margin-bottom: 12px;
        }
        .metric-label {
          font-size: 0.8rem;
          color: var(--muted) !important;
          margin-bottom: 3px;
        }
        .metric-value {
          color: var(--gold-soft) !important;
          font-size: 1.1rem;
          font-weight: 700;
        }
        .track-card {
          border: 1px solid rgba(212,175,55,0.30);
          border-radius: 16px;
          padding: 12px 14px;
          background: linear-gradient(160deg, rgba(212,175,55,0.08), rgba(23,23,23,0.98));
          min-height: 210px;
          box-shadow: 0 10px 25px rgba(0,0,0,0.35);
          margin-bottom: 14px;
        }
        .track-rank {
          font-size: 0.72rem;
          color: var(--gold-soft) !important;
          text-transform: uppercase;
          letter-spacing: 1.1px;
        }
        .track-title {
          font-size: 1.05rem;
          font-weight: 700;
          line-height: 1.25;
          margin-top: 5px;
          margin-bottom: 2px;
        }
        .track-artist {
          color: var(--muted) !important;
          margin-bottom: 10px;
        }
        .score-row {
          font-size: 0.86rem;
          color: var(--muted) !important;
          margin-top: 2px;
        }
        .pill {
          display: inline-block;
          border: 1px solid var(--border);
          border-radius: 999px;
          padding: 2px 8px;
          font-size: 0.72rem;
          color: var(--gold-soft) !important;
          margin-right: 5px;
          margin-top: 8px;
        }
        .action-title {
          font-size: 1.45rem;
          font-weight: 800;
          color: var(--gold-soft) !important;
          letter-spacing: 0.2px;
          margin-top: 6px;
          margin-bottom: 8px;
        }
        [data-testid="stSidebar"] {
          background: linear-gradient(180deg, #0c0c0c, #131313);
          border-right: 1px solid var(--border);
        }
        .stButton button, .stDownloadButton button {
          background: linear-gradient(180deg, #d4af37, #9b7a1d) !important;
          border: none !important;
          color: #101010 !important;
          border-radius: 10px !important;
          font-weight: 700 !important;
        }
        .stSelectbox div[data-baseweb="select"] > div,
        .stTextInput div[data-baseweb="input"] > div {
          background-color: var(--panel-2);
          border: 1px solid var(--border);
        }
        div[data-baseweb="popover"] ul,
        div[data-baseweb="select"] ul {
          background: var(--panel-2) !important;
          border: 1px solid rgba(212, 175, 55, 0.55) !important;
        }
        div[data-baseweb="popover"] li,
        div[data-baseweb="select"] li,
        div[data-baseweb="popover"] li span,
        div[data-baseweb="select"] li span {
          background: var(--panel-2) !important;
          color: var(--text) !important;
        }
        div[data-baseweb="popover"] li:hover,
        div[data-baseweb="select"] li:hover {
          background: #1f1f1f !important;
          color: var(--gold-soft) !important;
        }
        div[data-baseweb="popover"] li[aria-selected="true"],
        div[data-baseweb="select"] li[aria-selected="true"] {
          background: #232323 !important;
          color: var(--gold-soft) !important;
        }
        .platform-link {
          display: block;
          width: 100%;
          text-align: center;
          background: var(--panel-2);
          border: 1px solid var(--border);
          color: var(--text) !important;
          border-radius: 10px;
          text-decoration: none !important;
          padding: 8px 10px;
          font-weight: 600;
          margin-bottom: 6px;
        }
        .platform-link:hover {
          background: #1c1c1c;
          border: 1px solid rgba(212, 175, 55, 0.65);
          color: var(--gold-soft) !important;
        }
        [data-testid="stFileUploaderDropzone"] {
          background-color: var(--panel-2) !important;
          border: 1px dashed rgba(212, 175, 55, 0.55) !important;
          border-radius: 14px !important;
        }
        [data-testid="stFileUploaderDropzone"] > div {
          background-color: var(--panel-2) !important;
        }
        [data-testid="stFileUploaderDropzone"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stFileUploaderDropzone"] small,
        [data-testid="stFileUploaderDropzone"] span {
          color: var(--text) !important;
        }
        [data-testid="stFileUploaderDropzone"] button,
        [data-testid="stFileUploaderDropzone"] [data-testid="stBaseButton-secondary"] {
          background: linear-gradient(180deg, #d4af37, #9b7a1d) !important;
          border: none !important;
          color: #101010 !important;
          border-radius: 10px !important;
          font-weight: 700 !important;
        }
        [data-testid="stFileUploaderDropzone"] button:hover,
        [data-testid="stFileUploaderDropzone"] [data-testid="stBaseButton-secondary"]:hover {
          filter: brightness(1.03);
        }
        [data-testid="stDataFrame"] {
          border: 1px solid var(--border);
          border-radius: 14px;
          overflow: hidden;
          background: #000000;
          box-shadow: 0 8px 22px rgba(0,0,0,0.35);
          --gdg-bg-cell: #000000;
          --gdg-bg-cell-medium: #000000;
          --gdg-bg-header: #000000;
          --gdg-bg-header-has-focus: #000000;
          --gdg-text-dark: #f5f5f5;
          --gdg-text-medium: #e9e9e9;
          --gdg-text-light: #bfbfbf;
          --gdg-accent-color: #ffffff;
          --gdg-horizontal-border-color: rgba(212, 175, 55, 0.18);
          --gdg-vertical-border-color: rgba(212, 175, 55, 0.10);
        }
        [data-testid="stDataFrame"] canvas {
          background: #000000 !important;
        }
        [data-testid="stDataFrame"] [role="columnheader"] {
          background: #000000 !important;
          color: #f5f5f5 !important;
          font-weight: 700 !important;
          border-bottom: 1px solid rgba(212, 175, 55, 0.45) !important;
        }
        [data-testid="stDataFrame"] [role="gridcell"] {
          background: #000000 !important;
          color: #f5f5f5 !important;
          border-bottom: 1px solid rgba(212, 175, 55, 0.14) !important;
          border-right: 1px solid rgba(212, 175, 55, 0.08) !important;
        }
        [data-testid="stDataFrame"] [role="row"]:nth-child(even) [role="gridcell"] {
          background: #000000 !important;
        }
        [data-testid="stDataFrame"] [role="row"]:hover [role="gridcell"] {
          background: #0a0a0a !important;
        }
        [data-testid="stDataFrame"] [role="row"]:first-child [role="gridcell"] {
          border-top: 1px solid rgba(255, 77, 77, 0.55) !important;
        }
        [data-testid="stDataFrame"] div[role="progressbar"] > div {
          background: #ffffff !important;
        }
        .modern-debug-wrap {
          border: 1px solid rgba(212, 175, 55, 0.35);
          border-radius: 16px;
          overflow-x: auto;
          background: linear-gradient(160deg, rgba(212,175,55,0.08), rgba(8,8,8,0.98));
          box-shadow: 0 12px 28px rgba(0, 0, 0, 0.35);
        }
        .modern-debug-table {
          width: 100%;
          min-width: 1120px;
          border-collapse: collapse;
          color: #f5f5f5;
          font-size: 0.96rem;
        }
        .modern-debug-table thead th {
          text-align: left;
          position: sticky;
          top: 0;
          background: linear-gradient(180deg, rgba(212,175,55,0.30), rgba(30,24,10,0.95));
          color: #f5f5f5;
          border-bottom: 1px solid rgba(212, 175, 55, 0.45);
          padding: 12px 14px;
          letter-spacing: 0.2px;
          font-weight: 700;
          white-space: nowrap;
        }
        .modern-debug-table tbody td {
          padding: 12px 14px;
          border-bottom: 1px solid rgba(212, 175, 55, 0.12);
          vertical-align: middle;
        }
        .modern-debug-table tbody tr:nth-child(even) {
          background: rgba(255, 255, 255, 0.02);
        }
        .modern-debug-table tbody tr:hover {
          background: rgba(255, 77, 77, 0.10);
        }
        .debug-rank {
          color: #f1d98a;
          font-weight: 700;
          text-align: right;
          width: 56px;
        }
        .debug-track {
          color: #ffffff;
          font-weight: 600;
          max-width: 440px;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }
        .debug-subtle {
          color: #d9d9d9;
          white-space: nowrap;
        }
        .debug-score-cell {
          min-width: 190px;
        }
        .debug-score-wrap {
          display: flex;
          align-items: center;
          gap: 10px;
        }
        .debug-score-track {
          flex: 1;
          height: 8px;
          background: rgba(255, 255, 255, 0.16);
          border-radius: 999px;
          overflow: hidden;
          border: 1px solid rgba(255, 255, 255, 0.18);
        }
        .debug-score-fill {
          height: 100%;
          background: linear-gradient(90deg, #ff4d4d, #d4af37);
        }
        .debug-score-value {
          font-variant-numeric: tabular-nums;
          color: #ffffff;
          min-width: 42px;
          text-align: right;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def resolve_dataset_csv(dataset_path: Path) -> Path:
    csv_files = sorted(dataset_path.rglob("*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"No CSV files found under: {dataset_path}")
    if len(csv_files) == 1:
        return csv_files[0]

    for candidate in csv_files:
        name = candidate.name.lower()
        if "spotify" in name and "youtube" in name:
            return candidate
    return max(csv_files, key=lambda file_path: file_path.stat().st_size)


@st.cache_data(show_spinner=False)
def load_from_kagglehub(dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    try:
        import kagglehub
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError("kagglehub is not installed. Run: pip install kagglehub") from exc

    dataset_path = Path(kagglehub.dataset_download(dataset_id))
    csv_path = resolve_dataset_csv(dataset_path)
    return pd.read_csv(csv_path), str(csv_path), str(dataset_path)


@st.cache_data(show_spinner=False)
def prepare_music_data_cached(raw_df: pd.DataFrame) -> pd.DataFrame:
    return prepare_music_data(raw_df)


@st.cache_data(show_spinner=False)
def recommend_tracks_cached(
    data: pd.DataFrame,
    seed_weight_items: tuple[tuple[str, float], ...],
    mood: str,
    spotify_weight: float,
    discovery_mode: float,
    top_k: int,
    exclude_seed_tracks: bool,
    preferred_artist_weight_items: tuple[tuple[str, float], ...],
) -> pd.DataFrame:
    seed_weights = {str(name): float(weight) for name, weight in seed_weight_items}
    preferred_artist_weights = {str(name): float(weight) for name, weight in preferred_artist_weight_items}
    return recommend_tracks(
        data=data,
        seed_display_name=seed_weights,
        mood=mood,
        spotify_weight=spotify_weight,
        discovery_mode=discovery_mode,
        top_k=top_k,
        exclude_seed_tracks=exclude_seed_tracks,
        preferred_artist_weights=preferred_artist_weights,
    )


@st.cache_data(show_spinner=False)
def build_duration_playlist_cached(
    recommendations: pd.DataFrame,
    target_minutes: int,
    tolerance_minutes: int,
    candidate_limit: int,
    max_tracks: int,
) -> pd.DataFrame:
    return build_duration_playlist(
        recommendations=recommendations,
        target_minutes=target_minutes,
        tolerance_minutes=tolerance_minutes,
        candidate_limit=candidate_limit,
        max_tracks=max_tracks,
    )


@st.cache_data(show_spinner=False)
def get_seed_ui_options(data: pd.DataFrame) -> tuple[list[str], list[str], list[str], list[str]]:
    song_options = sorted(data["display_name"].astype(str).unique().tolist())
    artist_options = sorted(data["artist"].astype(str).unique().tolist())
    quick_top_songs = top_song_seed_options(data, top_n=8)
    quick_top_artists = top_artist_seed_options(data, top_n=8)["artist"].astype(str).tolist()
    return song_options, artist_options, quick_top_songs, quick_top_artists


def read_source(uploaded_file: Any, dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    if uploaded_file is not None:
        return pd.read_csv(uploaded_file), uploaded_file.name, "uploaded_file"
    return load_from_kagglehub(dataset_id)


def render_metric_card(label: str, value: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card">
          <div class="metric-label">{escape(str(label))}</div>
          <div class="metric-value">{escape(str(value))}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def format_track_duration(duration_ms: object) -> str:
    try:
        seconds = int(round(float(duration_ms) / 1000))
    except (TypeError, ValueError):
        return "0m 00s"
    minutes, sec = divmod(max(0, seconds), 60)
    return f"{minutes}m {sec:02d}s"


def format_total_duration(total_minutes: float) -> str:
    rounded_total = int(round(float(total_minutes)))
    hours, mins = divmod(max(0, rounded_total), 60)
    return f"{hours}hr {mins}mins (total {rounded_total} mins)"


def format_hours_minutes(total_minutes: float) -> str:
    rounded_total = int(round(float(total_minutes)))
    hours, mins = divmod(max(0, rounded_total), 60)
    return f"{hours}hr {mins}mins"


def extract_spotify_track_id(url: object) -> str | None:
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", str(url or ""))
    return match.group(1) if match else None


def extract_youtube_video_id(url: object) -> str | None:
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


def normalize_youtube_watch_url(url: object) -> str | None:
    video_id = extract_youtube_video_id(url)
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else None


def build_youtube_search_url(artist: object, track: object, artist_credits_json: object = "") -> str:
    artist_text = str(artist or "").strip()
    track_text = str(track or "").strip()
    query_parts: list[str] = []
    if track_text:
        query_parts.append(track_text)
    if artist_text:
        query_parts.append(artist_text)

    credits_raw = str(artist_credits_json or "").strip()
    if credits_raw:
        try:
            credits = json.loads(credits_raw)
        except (TypeError, ValueError):
            credits = None
        if isinstance(credits, list):
            for credit in credits:
                credit_text = str(credit or "").strip()
                if not credit_text:
                    continue
                if credit_text.lower() in {part.lower() for part in query_parts}:
                    continue
                query_parts.append(credit_text)
                if len(query_parts) >= 4:
                    break

    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return "https://www.youtube.com/"
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def _is_youtube_embed_likely_available(watch_url: str) -> bool:
    # Quick probe with per-session TTL cache to avoid repeatedly rendering broken embeds.
    # Negative results expire quickly so temporarily removed/blocked checks are revalidated.
    cache_version = 3
    if st.session_state.get("_youtube_embed_probe_cache_version") != cache_version:
        st.session_state["_youtube_embed_probe_cache"] = {}
        st.session_state["_youtube_embed_probe_cache_version"] = cache_version
    cache = st.session_state.setdefault("_youtube_embed_probe_cache", {})
    key = str(watch_url or "").strip()
    if not key:
        return False
    now_ts = time.time()
    cached = cache.get(key)
    if isinstance(cached, dict):
        cached_ok = bool(cached.get("ok", False))
        cached_ts = float(cached.get("ts", 0.0) or 0.0)
        max_age = 3600.0 if cached_ok else 120.0
        if now_ts - cached_ts <= max_age:
            return cached_ok
    elif isinstance(cached, bool):
        # Backward-compatible cache format from earlier runs.
        return bool(cached)

    video_id = extract_youtube_video_id(key)
    if not video_id:
        cache[key] = {"ok": False, "ts": now_ts}
        return False

    # Probe 1: YouTube playability status (includes playableInEmbed flags when available).
    # This alone is not sufficient for embedding, but it can disqualify a video.
    playability_ok: bool | None = None
    playable_in_embed_flag: bool | None = None
    info_url = f"https://www.youtube.com/get_video_info?video_id={video_id}&el=detailpage&hl=en"
    info_request = Request(
        info_url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/plain",
        },
    )
    try:
        with urlopen(info_request, timeout=4) as response:
            payload = response.read().decode("utf-8", errors="ignore")
        parsed = parse_qs(payload)
        player_response_raw = parsed.get("player_response", [None])[0]
        if player_response_raw:
            player_response = json.loads(player_response_raw)
            playability = player_response.get("playabilityStatus", {}) if isinstance(player_response, dict) else {}
            status = str(playability.get("status", "")).upper()
            playable_in_embed = playability.get("playableInEmbed")
            if playable_in_embed is False:
                cache[key] = {"ok": False, "ts": now_ts}
                return False
            if isinstance(playable_in_embed, bool):
                playable_in_embed_flag = playable_in_embed
            if status in {"UNPLAYABLE", "ERROR", "LOGIN_REQUIRED"}:
                cache[key] = {"ok": False, "ts": now_ts}
                return False
            if status == "OK":
                playability_ok = True
    except Exception:
        # Fall back to oEmbed probe below.
        pass

    # Probe 2: oEmbed endpoint.
    probe_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    request = Request(
        probe_url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    oembed_ok = False
    try:
        with urlopen(request, timeout=4) as response:
            oembed_ok = int(getattr(response, "status", 200) or 200) == 200
    except HTTPError as exc:
        # 401/403/404 are common for unavailable/unembeddable/private videos.
        if exc.code in {401, 403, 404, 410}:
            cache[key] = {"ok": False, "ts": now_ts}
            return False
        cache[key] = {"ok": False, "ts": now_ts}
        return False
    except URLError:
        cache[key] = {"ok": False, "ts": now_ts}
        return False
    except Exception:
        cache[key] = {"ok": False, "ts": now_ts}
        return False

    # Final decision is conservative but not over-strict:
    # - require oEmbed success
    # - reject only when playability is explicitly bad
    # - reject only when playableInEmbed is explicitly false
    final_ok = bool(
        oembed_ok
        and playability_ok is not False
        and playable_in_embed_flag is not False
    )
    cache[key] = {"ok": final_ok, "ts": now_ts}
    return final_ok


def _is_youtube_video_playlist_playable(video_id: str) -> bool:
    key = str(video_id or "").strip()
    if not key:
        return False
    cache = st.session_state.setdefault("_youtube_playlist_playable_cache", {})
    if key in cache:
        return bool(cache[key])
    # Playlist compatibility is broader than iframe embed compatibility.
    # Use playability status and ignore playableInEmbed restrictions here.
    info_url = f"https://www.youtube.com/get_video_info?video_id={key}&el=detailpage&hl=en"
    request = Request(
        info_url,
        method="GET",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/plain",
        },
    )
    try:
        with urlopen(request, timeout=4) as response:
            payload = response.read().decode("utf-8", errors="ignore")
        parsed = parse_qs(payload)
        player_response_raw = parsed.get("player_response", [None])[0]
        if not player_response_raw:
            cache[key] = True
            return True
        player_response = json.loads(player_response_raw)
        playability = player_response.get("playabilityStatus", {}) if isinstance(player_response, dict) else {}
        status = str(playability.get("status", "")).upper()
        if status in {"UNPLAYABLE", "ERROR", "LOGIN_REQUIRED"}:
            cache[key] = False
            return False
        cache[key] = True
        return True
    except Exception:
        # Fail open for playlist generation to avoid dropping all rows on transient
        # network/API parsing issues.
        cache[key] = True
        return True


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _should_try_live_youtube_override(row: pd.Series, direct_watch: str | None) -> bool:
    # Always try live resolution when we don't have a direct watch URL.
    if not direct_watch:
        return True

    title_text = str(row.get("title", "") or "").lower()
    channel_text = str(row.get("channel", "") or "").lower()
    title_signal = any(
        token in title_text
        for token in (
            "[mv]",
            " m/v",
            " mv",
            "music video",
            "official video",
            "official audio",
            "performance video",
            "visualizer",
        )
    )
    official_signal = (
        _as_bool(row.get("yt_title_official_mv"))
        or _as_bool(row.get("yt_title_official_audio"))
        or ("official" in title_text)
    )
    channel_match = _as_float(row.get("yt_channel_artist_match"), 0.0) > 0
    trusted_channel = any(
        token in channel_text
        for token in (
            "vevo",
            "1thek",
            "jyp entertainment",
            "smtown",
            "hybe labels",
            "starshiptv",
            "stone music entertainment",
            "yg entertainment",
            "genie music",
            "kq entertainment",
            "rbw",
            "cube entertainment",
            "pledis entertainment",
            "big hit labels",
            "ygex",
        )
    ) or channel_text.endswith(" - topic")
    suspicious_channel = any(
        token in channel_text
        for token in (
            "hard to find",
            "lyrics",
            "lyric",
            "fan",
            "karaoke",
            "sped up",
            "slowed",
            "nightcore",
            "bass boosted",
            "edit",
        )
    )
    strong_direct = official_signal or title_signal or channel_match or trusted_channel
    return (not strong_direct) or suspicious_channel


def resolve_live_youtube_watch_url(
    artist: str,
    track: str,
    artist_credits_json: str = "",
    timeout: int = 8,
) -> tuple[str, str, float]:
    if resolve_best_youtube_live is None:
        return "", "resolver unavailable", 0.0

    result = resolve_best_youtube_live(
        artist_owner=artist,
        track=track,
        artist_credits_json=artist_credits_json,
        max_results=10,
        timeout=timeout,
    )
    return (
        str(result.get("watch_url", "") or ""),
        str(result.get("reason", "") or ""),
        float(result.get("score", 0.0) or 0.0),
    )


def prefer_direct_youtube_url(youtube_link: object, fallback_url: object) -> str:
    direct_from_link = normalize_youtube_watch_url(youtube_link)
    if direct_from_link:
        return direct_from_link
    direct_from_fallback = normalize_youtube_watch_url(fallback_url)
    if direct_from_fallback:
        return direct_from_fallback
    return str(youtube_link or fallback_url or "").strip()


def curated_youtube_watch_override(row: pd.Series) -> str:
    canonical_key = str(row.get("canonical_key", "") or "").strip()
    if canonical_key and canonical_key in CURATED_YOUTUBE_WATCH_OVERRIDES:
        return CURATED_YOUTUBE_WATCH_OVERRIDES[canonical_key]
    uri = str(row.get("uri", "") or "").strip()
    if uri.startswith("spotify:track:"):
        spotify_key = f"sp:{uri.split(':')[-1]}"
        return CURATED_YOUTUBE_WATCH_OVERRIDES.get(spotify_key, "")
    return ""


def resolve_playback_youtube_url(row: pd.Series) -> str:
    direct_watch = curated_youtube_watch_override(row) or normalize_youtube_watch_url(row.get("youtube_link")) or normalize_youtube_watch_url(row.get("url_youtube"))
    search_fallback = build_youtube_search_url(
        row.get("artist", ""),
        row.get("track", ""),
        row.get("artist_credits", ""),
    )
    # For row-level playback, trust the current dataset's direct watch URL when it exists.
    # The queue-level temp playlist has its own playability logic and can still resolve
    # missing/unplayable rows independently without changing the selected-track target.
    if direct_watch:
        return direct_watch

    live_watch, _, live_score = resolve_live_youtube_watch_url(
        str(row.get("artist", "")),
        str(row.get("track", "")),
        str(row.get("artist_credits", "")),
    )
    if live_watch and live_score >= 8.0:
        return live_watch

    fallback = str(row.get("youtube_link") or row.get("url_youtube") or "").strip()
    return fallback or search_fallback


def resolve_playback_youtube_targets(
    row: pd.Series,
    force_live: bool = False,
    live_timeout: int = 8,
) -> tuple[str, str]:
    """
    Returns (link_url, embed_url).
    - link_url: URL to open via YouTube button.
    - embed_url: URL safe enough to attempt st.video embed.
    """
    direct_watch = curated_youtube_watch_override(row) or normalize_youtube_watch_url(row.get("youtube_link")) or normalize_youtube_watch_url(row.get("url_youtube"))
    search_fallback = build_youtube_search_url(
        row.get("artist", ""),
        row.get("track", ""),
        row.get("artist_credits", ""),
    )

    # For single-track playback, keep the snapshot's direct YouTube target stable when present.
    if direct_watch and not force_live:
        return direct_watch, direct_watch

    if force_live or not direct_watch:
        live_watch, _, live_score = resolve_live_youtube_watch_url(
            str(row.get("artist", "")),
            str(row.get("track", "")),
            str(row.get("artist_credits", "")),
            timeout=live_timeout,
        )
        if live_watch and (force_live or live_score >= 8.0):
            return live_watch, live_watch

    # Keep direct URL as clickable link and always attempt inline playback.
    # Some YouTube probes can produce false negatives even when st.video can play.
    if direct_watch:
        return direct_watch, direct_watch

    fallback = str(row.get("youtube_link") or row.get("url_youtube") or "").strip()
    if fallback:
        return fallback, ""
    return search_fallback, ""


def build_playable_youtube_ids(
    queue: pd.DataFrame,
    max_ids: int = 50,
    max_live_resolves: int = 12,
    live_timeout: int = 4,
    min_ids_required: int = 2,
    fill_to_max: bool = False,
    max_total_seconds: float = 12.0,
    return_stats: bool = False,
) -> list[str] | tuple[list[str], dict[str, Any]]:
    cache: dict[tuple[object, ...], object] = st.session_state.setdefault(
        "_playable_youtube_ids_cache",
        {},
    )
    signature = tuple(
        (
            str(row.get("artist", "")),
            str(row.get("track", "")),
            str(row.get("artist_credits", "")),
        )
        for _, row in queue.iterrows()
    )
    cache_key = (
        signature,
        int(max_ids),
        int(max_live_resolves),
        int(live_timeout),
        int(min_ids_required),
        bool(fill_to_max),
        float(max_total_seconds),
    )
    cached_payload = cache.get(cache_key)
    cache_ttl_seconds = 180.0
    if isinstance(cached_payload, dict):
        cached_at = float(cached_payload.get("cached_at", 0.0) or 0.0)
        if cached_at and (time.time() - cached_at > cache_ttl_seconds):
            cached_payload = None
    if cached_payload is not None:
        if isinstance(cached_payload, dict):
            cached_ids = list(cached_payload.get("ids", []))
            cached_stats = dict(cached_payload.get("stats", {}))
        else:
            # Backward compatibility for older cache entries that stored list only.
            cached_ids = list(cached_payload)  # type: ignore[arg-type]
            cached_stats = {}
        if return_stats:
            default_stats = {
                "playable_count": len(cached_ids),
                "target_rows": min(len(queue), max_ids),
                "included_direct": 0,
                "included_resolved": 0,
                "resolver_attempted": 0,
                "resolver_resolved": 0,
                "duplicate_rows": 0,
                "unresolved_rows": 0,
                "budget_blocked_rows": 0,
                "resolve_budget": int(max_live_resolves if fill_to_max else min(max_live_resolves, max_ids)),
                "row_diagnostics": [],
            }
            default_stats.update({k: int(v) for k, v in cached_stats.items() if isinstance(v, int)})
            if isinstance(cached_stats.get("row_diagnostics"), list):
                default_stats["row_diagnostics"] = list(cached_stats.get("row_diagnostics", []))
            return cached_ids, default_stats
        return cached_ids

    youtube_ids: list[str] = []
    target_rows = min(len(queue), max_ids)
    if target_rows <= 0:
        empty_stats = {
            "playable_count": 0,
            "playable_linked_rows": 0,
            "target_rows": 0,
            "included_direct": 0,
            "included_resolved": 0,
            "resolver_attempted": 0,
            "resolver_resolved": 0,
            "duplicate_rows": 0,
            "unresolved_rows": 0,
            "budget_blocked_rows": 0,
            "resolve_budget": 0,
            "row_diagnostics": [],
        }
        if return_stats:
            return [], empty_stats
        return []
    direct_ids_by_pos: dict[int, str] = {}
    unresolved_rows_by_pos: dict[int, pd.Series] = {}
    resolved_ids_by_pos: dict[int, str] = {}
    start_ts = time.time()

    def _time_budget_exceeded() -> bool:
        if max_total_seconds <= 0:
            return False
        return (time.time() - start_ts) >= max_total_seconds

    # Pass 1: collect direct video IDs from ranked rows only.
    for pos in range(target_rows):
        row = queue.iloc[pos]
        direct_watch = (
            curated_youtube_watch_override(row)
            or normalize_youtube_watch_url(row.get("youtube_url"))
            or normalize_youtube_watch_url(row.get("youtube_link"))
            or normalize_youtube_watch_url(row.get("url_youtube"))
        )
        video_id = extract_youtube_video_id(direct_watch) if direct_watch else None
        if video_id:
            direct_ids_by_pos[pos] = video_id
        else:
            unresolved_rows_by_pos[pos] = row

    # Pass 2: preserve queue/debug ranking order and resolve missing rows inline.
    live_resolves = 0
    resolve_budget = max_live_resolves if fill_to_max else min(max_live_resolves, target_rows)
    included_direct = 0
    included_resolved = 0
    resolver_attempted = 0
    resolver_resolved = 0
    duplicate_rows = 0
    unresolved_rows = 0
    budget_blocked_rows = 0
    row_diag_by_pos: dict[int, dict[str, str]] = {}
    for pos in range(target_rows):
        row_for_label = queue.iloc[pos]
        track_artist = f'{str(row_for_label.get("artist", ""))} - {str(row_for_label.get("track", ""))}'.strip(" -")
        row_diag_by_pos[pos] = {
            "rank": str(pos + 1),
            "track_artist": track_artist or "-",
            "playlist_status": "skipped",
            "playlist_source": "-",
            "skip_reason": "no_direct_id",
            "skip_detail": "-",
            "youtube_id": "-",
        }
    for pos in range(target_rows):
        video_id = direct_ids_by_pos.get(pos)
        source = "direct" if video_id else ""
        live_reason = ""
        live_score = 0.0
        live_attempted = False
        if (
            not video_id
            and live_resolves < resolve_budget
            and pos in unresolved_rows_by_pos
            and not _time_budget_exceeded()
        ):
            row = unresolved_rows_by_pos[pos]
            live_attempted = True
            resolver_attempted += 1
            live_watch, live_reason, live_score = resolve_live_youtube_watch_url(
                str(row.get("artist", "")),
                str(row.get("track", "")),
                str(row.get("artist_credits", "")),
                timeout=live_timeout,
            )
            live_resolves += 1
            if live_watch and live_score >= 8.0:
                video_id = extract_youtube_video_id(live_watch)
                if video_id:
                    resolved_ids_by_pos[pos] = video_id
                    source = "resolved"
                    resolver_resolved += 1

        if not video_id or video_id in youtube_ids:
            if not video_id:
                unresolved_rows += 1
                if pos in unresolved_rows_by_pos and live_attempted and (not live_reason):
                    row_diag_by_pos[pos].update(
                        {
                            "playlist_source": "resolved",
                            "skip_reason": "no_result",
                            "skip_detail": "-",
                            "youtube_id": "-",
                        }
                    )
                elif pos in unresolved_rows_by_pos and live_attempted and live_score < 8.0:
                    row_diag_by_pos[pos].update(
                        {
                            "playlist_source": "resolved",
                            "skip_reason": "low_confidence",
                            "skip_detail": f"score={live_score:.2f}",
                            "youtube_id": "-",
                        }
                    )
                elif pos in unresolved_rows_by_pos and live_resolves >= resolve_budget:
                    budget_blocked_rows += 1
                    row_diag_by_pos[pos].update(
                        {
                            "skip_reason": "budget_limit",
                            "skip_detail": f"budget={resolve_budget}",
                            "youtube_id": "-",
                        }
                    )
                else:
                    if _time_budget_exceeded():
                        row_diag_by_pos[pos].update(
                            {
                                "skip_reason": "runtime_budget",
                                "skip_detail": f"seconds={max_total_seconds:.1f}",
                                "youtube_id": "-",
                            }
                        )
                    else:
                        row_diag_by_pos[pos].update(
                            {
                                "skip_reason": "no_direct_id",
                                "skip_detail": "-",
                                "youtube_id": "-",
                            }
                        )
            else:
                duplicate_rows += 1
                row_diag_by_pos[pos].update(
                    {
                        "playlist_source": source or "direct",
                        "skip_reason": "duplicate",
                        "skip_detail": video_id,
                        "youtube_id": video_id or "-",
                    }
                )
            continue
        youtube_ids.append(video_id)
        row_diag_by_pos[pos].update(
            {
                "playlist_status": "linked",
                "playlist_source": source or "direct",
                "skip_reason": "-",
                "skip_detail": "-",
                "youtube_id": video_id or "-",
            }
        )
        if source == "direct":
            included_direct += 1
        elif source == "resolved":
            included_resolved += 1
        if len(youtube_ids) >= max_ids:
            break

    # Pass 3: validate linked IDs for final playlist URL compatibility.
    # If any linked ID is unplayable, mark it and try to backfill in rank order.
    for pos in range(target_rows):
        diag = row_diag_by_pos.get(pos)
        if not diag or str(diag.get("playlist_status")) != "linked":
            continue
        if str(diag.get("playlist_source", "")) == "direct":
            # Direct IDs are trusted enough for fast temporary playlist creation.
            continue
        if _time_budget_exceeded():
            break
        video_id = str(diag.get("youtube_id", "")).strip()
        if not video_id or video_id == "-":
            continue
        if _is_youtube_video_playlist_playable(video_id):
            continue
        diag.update(
            {
                "playlist_status": "skipped",
                "skip_reason": "unplayable_on_youtube_playlist",
                "skip_detail": video_id,
            }
        )

    # Rebuild linked IDs after validation in queue/debug order.
    youtube_ids = []
    for pos in range(target_rows):
        diag = row_diag_by_pos.get(pos, {})
        if str(diag.get("playlist_status")) != "linked":
            continue
        video_id = str(diag.get("youtube_id", "")).strip()
        if not video_id or video_id == "-" or video_id in youtube_ids:
            continue
        youtube_ids.append(video_id)

    # Backfill skipped rows in ranking order using resolver so we can recover
    # from dropped/unplayable/duplicate rows without reordering visible rows.
    if len(youtube_ids) < target_rows:
        for pos in range(target_rows):
            if len(youtube_ids) >= target_rows:
                break
            if _time_budget_exceeded():
                break
            diag = row_diag_by_pos.get(pos)
            if not diag or str(diag.get("playlist_status")) == "linked":
                continue
            if live_resolves >= max_live_resolves:
                if str(diag.get("skip_reason", "")) in {"-", "", "no_direct_id"}:
                    diag.update({"skip_reason": "budget_limit", "skip_detail": f"budget={max_live_resolves}"})
                budget_blocked_rows += 1
                continue
            row = queue.iloc[pos]
            live_watch, live_reason, live_score = resolve_live_youtube_watch_url(
                str(row.get("artist", "")),
                str(row.get("track", "")),
                str(row.get("artist_credits", "")),
                timeout=live_timeout,
            )
            live_resolves += 1
            resolver_attempted += 1
            if not live_watch:
                diag.update({"playlist_source": "resolved_backfill", "skip_reason": "no_result", "skip_detail": "-"})
                continue
            if live_score < 8.0:
                diag.update(
                    {
                        "playlist_source": "resolved_backfill",
                        "skip_reason": "low_confidence",
                        "skip_detail": f"score={live_score:.2f}",
                    }
                )
                continue
            candidate_id = extract_youtube_video_id(live_watch)
            if not candidate_id:
                diag.update({"playlist_source": "resolved_backfill", "skip_reason": "no_result", "skip_detail": "-"})
                continue
            if not _is_youtube_video_playlist_playable(candidate_id):
                diag.update(
                    {
                        "playlist_source": "resolved_backfill",
                        "skip_reason": "unplayable_on_youtube_playlist",
                        "skip_detail": candidate_id,
                        "youtube_id": candidate_id,
                    }
                )
                continue
            diag.update(
                {
                    "playlist_status": "linked",
                    "playlist_source": "resolved_backfill",
                    "skip_reason": "-",
                    "skip_detail": "-",
                    "youtube_id": candidate_id,
                }
            )
            resolver_resolved += 1
            included_resolved += 1

    # Final materialization in strict rank order.
    final_ids: list[str] = []
    for pos in range(target_rows):
        diag = row_diag_by_pos.get(pos)
        if not diag or str(diag.get("playlist_status")) != "linked":
            continue
        video_id = str(diag.get("youtube_id", "")).strip()
        if not video_id or video_id == "-":
            diag.update({"playlist_status": "skipped", "skip_reason": "no_result", "skip_detail": "-"})
            continue
        if video_id in final_ids:
            diag.update({"playlist_status": "skipped", "skip_reason": "duplicate", "skip_detail": video_id})
            continue
        final_ids.append(video_id)
        if len(final_ids) >= max_ids:
            break

    youtube_ids = final_ids

    row_diagnostics = [row_diag_by_pos[pos] for pos in range(target_rows) if pos in row_diag_by_pos]
    linked_rows = sum(1 for item in row_diagnostics if str(item.get("playlist_status", "")) == "linked")
    included_direct = sum(
        1
        for item in row_diagnostics
        if str(item.get("playlist_status", "")) == "linked" and str(item.get("playlist_source", "")) == "direct"
    )
    included_resolved = sum(
        1
        for item in row_diagnostics
        if str(item.get("playlist_status", "")) == "linked"
        and str(item.get("playlist_source", "")) in {"resolved", "resolved_backfill"}
    )
    reason_counts: dict[str, int] = {}
    for item in row_diagnostics:
        reason = str(item.get("skip_reason", "-")).strip() or "-"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    duplicate_rows = int(reason_counts.get("duplicate", 0))
    budget_blocked_rows = int(reason_counts.get("budget_limit", 0))
    unresolved_rows = sum(
        int(reason_counts.get(key, 0))
        for key in (
            "no_result",
            "no_direct_id",
            "low_confidence",
            "unplayable_on_youtube_playlist",
            "runtime_budget",
        )
    )
    stats = {
        "playable_count": int(len(youtube_ids)),
        "playable_linked_rows": int(linked_rows),
        "target_rows": int(target_rows),
        "included_direct": int(included_direct),
        "included_resolved": int(included_resolved),
        "resolver_attempted": int(resolver_attempted),
        "resolver_resolved": int(resolver_resolved),
        "duplicate_rows": int(duplicate_rows),
        "unresolved_rows": int(unresolved_rows),
        "budget_blocked_rows": int(budget_blocked_rows),
        "resolve_budget": int(resolve_budget),
        "runtime_budget_exceeded": bool(_time_budget_exceeded()),
        "runtime_seconds": float(round(time.time() - start_ts, 3)),
        "row_diagnostics": row_diagnostics,
    }
    # Cache both complete and partial builds for a short TTL to keep UI responsive.
    cache[cache_key] = {"ids": list(youtube_ids), "stats": stats, "cached_at": time.time()}
    if return_stats:
        return youtube_ids, stats
    return youtube_ids


def render_platform_link(label: str, url: str) -> None:
    st.markdown(
        f'<a class="platform-link" href="{url}" target="_blank" rel="noopener noreferrer">{label}</a>',
        unsafe_allow_html=True,
    )


def render_track_links(spotify_url: object, youtube_url: object) -> None:
    cols = st.columns(2)
    spotify_text = str(spotify_url or "").strip()
    youtube_text = str(youtube_url or "").strip()

    with cols[0]:
        if spotify_text:
            render_platform_link("Spotify", spotify_text)
    with cols[1]:
        if youtube_text:
            render_platform_link("YouTube", youtube_text)


def render_modern_debug_table(playlist: pd.DataFrame) -> None:
    if playlist.empty:
        st.caption("No debug rows to display.")
        return

    safe_playlist = playlist.copy()
    for numeric_col in ("recommendation_score", "similarity_score", "platform_score", "discovery_score", "momentum_score"):
        safe_playlist[numeric_col] = pd.to_numeric(safe_playlist[numeric_col], errors="coerce").fillna(0.0)

    def score_cell(value: float) -> str:
        clipped = float(np.clip(value, 0.0, 1.0))
        pct = clipped * 100
        return (
            '<td class="debug-score-cell"><div class="debug-score-wrap">'
            f'<div class="debug-score-track"><div class="debug-score-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="debug-score-value">{clipped:.3f}</span>'
            "</div></td>"
        )

    rows_html: list[str] = []
    for idx, row in safe_playlist.reset_index(drop=True).iterrows():
        rows_html.append(
            "<tr>"
            f'<td class="debug-rank">{idx + 1}</td>'
            f'<td class="debug-subtle">{escape(str(row.get("artist", "")))}</td>'
            f'<td class="debug-track">{escape(str(row.get("track", "")))}</td>'
            f'<td class="debug-subtle">{escape(format_track_duration(row.get("duration_ms", 0)))}</td>'
            f"{score_cell(float(row.get('recommendation_score', 0.0)))}"
            f"{score_cell(float(row.get('momentum_score', 0.0)))}"
            f'<td class="debug-subtle">{escape(format_compact_number(float(row.get("views", 0) or 0)))}</td>'
            f'<td class="debug-subtle">{escape(format_compact_number(float(row.get("stream", 0) or 0)))}</td>'
            "</tr>"
        )

    st.markdown(
        (
            '<div class="modern-debug-wrap"><table class="modern-debug-table">'
            "<thead><tr>"
            "<th>Rank</th><th>Artist</th><th>Track</th><th>Duration</th><th>Match</th><th>Momentum</th><th>Views</th><th>Streams</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody></table></div>"
        ),
        unsafe_allow_html=True,
    )


def top_artist_seed_options(data: pd.DataFrame, top_n: int = 5) -> pd.DataFrame:
    artist_rank = data.groupby("artist", as_index=False).agg(
        artist_streams=("stream", "sum"),
        artist_views=("views", "sum"),
        artist_momentum=("momentum_score", "mean"),
    )
    artist_rank["artist_score"] = (
        np.log1p(artist_rank["artist_streams"]) + np.log1p(artist_rank["artist_views"]) + 2.0 * artist_rank["artist_momentum"]
    )

    best_track_per_artist = data.sort_values(["momentum_score", "views", "stream"], ascending=False).drop_duplicates(
        subset=["artist"], keep="first"
    )
    options = artist_rank.merge(best_track_per_artist[["artist", "display_name"]], on="artist", how="left")
    options = options.sort_values(["artist_score", "artist"], ascending=[False, True]).head(top_n)
    return options.reset_index(drop=True)


def top_song_seed_options(data: pd.DataFrame, top_n: int = 5) -> list[str]:
    top_songs = data.sort_values(["momentum_score", "views", "stream"], ascending=False).head(top_n)
    return top_songs["display_name"].tolist()


def render_toggle_chips(
    labels: list[str],
    selected_values: list[str],
    key_prefix: str,
    min_chips: int = 5,
    chips_per_row: int = 3,
) -> str | None:
    if not labels:
        return None

    chip_count = max(min_chips, min(len(labels), 8))
    options = labels[:chip_count]
    clicked = None

    for row_start in range(0, len(options), chips_per_row):
        row_labels = options[row_start : row_start + chips_per_row]
        columns = st.columns(len(row_labels))
        for offset, label in enumerate(row_labels):
            idx = row_start + offset
            chip_label = f"✓ {label}" if label in selected_values else label
            if columns[offset].button(chip_label, key=f"{key_prefix}_{idx}_{label}", use_container_width=True):
                clicked = label
    return clicked


def best_track_for_artist(data: pd.DataFrame, artist_name: str) -> str | None:
    artist_rows = data.loc[data["artist"] == artist_name].copy()

    artist_key = re.sub(r"[^a-z0-9 ]+", " ", str(artist_name).lower()).strip()
    featured_rows = data.head(0).copy()
    if artist_key and "featured_artist_norms" in data.columns:
        featured_mask = data["featured_artist_norms"].fillna("").astype(str).str.contains(
            rf"(^|\|){re.escape(artist_key)}(\||$)",
            regex=True,
        )
        featured_rows = data.loc[featured_mask].copy()

    candidate_rows = pd.concat([artist_rows, featured_rows], ignore_index=False)
    candidate_rows = candidate_rows.drop_duplicates(subset=["display_name"], keep="first")
    if candidate_rows.empty:
        return None

    best = candidate_rows.sort_values(["momentum_score", "views", "stream"], ascending=False).iloc[0]
    return str(best["display_name"])


def personality_seed_option(data: pd.DataFrame, profile_name: str) -> tuple[str, str]:
    profile = PERSONALITY_PROFILES[profile_name]
    targets = profile["targets"]
    score = pd.Series(np.zeros(len(data), dtype=float), index=data.index)

    for feature, target_value in targets.items():
        if feature not in data.columns:
            continue
        score += np.abs(pd.to_numeric(data[feature], errors="coerce").fillna(0.0) - float(target_value))

    best = (
        data.assign(personality_distance=score)
        .sort_values(["personality_distance", "momentum_score", "views", "stream"], ascending=[True, False, False, False])
        .iloc[0]
    )
    return str(best["display_name"]), str(profile["description"])


def _default_seed_track(data: pd.DataFrame) -> str:
    return str(data.sort_values(["momentum_score", "views", "stream"], ascending=False).iloc[0]["display_name"])


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _init_seed_selection_state(data: pd.DataFrame) -> None:
    if "selected_seed_artists" not in st.session_state:
        st.session_state["selected_seed_artists"] = []
    if "selected_seed_songs" not in st.session_state:
        st.session_state["selected_seed_songs"] = []

    valid_artists = set(data["artist"].astype(str))
    valid_songs = set(data["display_name"].astype(str))

    st.session_state["selected_seed_artists"] = [
        artist
        for artist in _dedupe_preserve_order([str(item) for item in st.session_state["selected_seed_artists"]])
        if artist in valid_artists
    ]
    st.session_state["selected_seed_songs"] = [
        song
        for song in _dedupe_preserve_order([str(item) for item in st.session_state["selected_seed_songs"]])
        if song in valid_songs
    ]


def _current_combined_seed_count() -> int:
    return len(st.session_state.get("selected_seed_artists", [])) + len(st.session_state.get("selected_seed_songs", []))


def _toggle_seed_selection(list_key: str, value: str, max_total: int = MAX_SEED_SELECTIONS) -> None:
    selected = list(st.session_state.get(list_key, []))
    if value in selected:
        selected = [item for item in selected if item != value]
        st.session_state[list_key] = selected
        return

    if _current_combined_seed_count() >= max_total:
        st.warning(f"You can select at most {max_total} picks total across songs and artists.")
        return

    selected.append(value)
    st.session_state[list_key] = _dedupe_preserve_order(selected)


def _add_seed_selection(list_key: str, value: str, max_total: int = MAX_SEED_SELECTIONS) -> None:
    selected = list(st.session_state.get(list_key, []))
    if value in selected:
        return
    if _current_combined_seed_count() >= max_total:
        st.warning(f"You can select at most {max_total} picks total across songs and artists.")
        return
    selected.append(value)
    st.session_state[list_key] = _dedupe_preserve_order(selected)


def _render_final_pick_box() -> None:
    selected_artists = list(st.session_state.get("selected_seed_artists", []))
    selected_songs = list(st.session_state.get("selected_seed_songs", []))
    all_labels = [f"Artist: {name}" for name in selected_artists] + [f"Song: {name}" for name in selected_songs]

    retained_labels = st.multiselect(
        "Final Pick Box",
        options=all_labels,
        default=all_labels,
        placeholder="Selected songs/artists appear here (remove with x).",
    )
    retained_set = set(retained_labels)
    st.session_state["selected_seed_artists"] = [
        name for name in selected_artists if f"Artist: {name}" in retained_set
    ]
    st.session_state["selected_seed_songs"] = [
        name for name in selected_songs if f"Song: {name}" in retained_set
    ]

    final_count = _current_combined_seed_count()
    st.caption(f"Final Picks ({final_count}/{MAX_SEED_SELECTIONS})")


def _build_seed_weights_from_state(data: pd.DataFrame) -> dict[str, float]:
    selected_artists = list(st.session_state.get("selected_seed_artists", []))
    selected_songs = list(st.session_state.get("selected_seed_songs", []))
    total_selected = len(selected_artists) + len(selected_songs)

    if total_selected == 0:
        fallback_seed = _default_seed_track(data)
        st.info(f"No picks selected. Using default starter: {fallback_seed}")
        return {fallback_seed: 1.0}

    artist_share = len(selected_artists) / total_selected
    song_share = len(selected_songs) / total_selected
    st.caption(f"Blend mix: Artists ({artist_share:.0%}) + Songs ({song_share:.0%})")

    unit_weight = 1.0 / total_selected
    seed_weights: dict[str, float] = {}
    for artist_name in selected_artists:
        artist_seed = best_track_for_artist(data, artist_name)
        if artist_seed:
            seed_weights[artist_seed] = seed_weights.get(artist_seed, 0.0) + unit_weight
    for song_name in selected_songs:
        seed_weights[song_name] = seed_weights.get(song_name, 0.0) + unit_weight

    total_weight = float(sum(seed_weights.values()))
    if total_weight <= 0:
        return {_default_seed_track(data): 1.0}
    return {name: weight / total_weight for name, weight in seed_weights.items()}


def _build_preferred_artist_weights(data: pd.DataFrame) -> dict[str, float]:
    artist_counts: dict[str, float] = {}

    for artist_name in st.session_state.get("selected_seed_artists", []):
        key = str(artist_name).strip()
        if key:
            artist_counts[key] = artist_counts.get(key, 0.0) + 1.0

    selected_songs = [str(song).strip() for song in st.session_state.get("selected_seed_songs", []) if str(song).strip()]
    if selected_songs:
        song_artist_lookup = (
            data.loc[data["display_name"].isin(selected_songs), ["display_name", "artist"]]
            .drop_duplicates(subset=["display_name"], keep="first")
            .set_index("display_name")["artist"]
            .to_dict()
        )
        for song_name in selected_songs:
            artist_name = str(song_artist_lookup.get(song_name, "")).strip()
            if artist_name:
                artist_counts[artist_name] = artist_counts.get(artist_name, 0.0) + 1.0

    total = float(sum(artist_counts.values()))
    if total <= 0:
        return {}
    return {artist: (count / total) for artist, count in artist_counts.items()}


def render_seed_experience(data: pd.DataFrame) -> tuple[dict[str, float], dict[str, Any]]:
    _init_seed_selection_state(data)
    song_options, artist_options, quick_top_songs, quick_top_artists = get_seed_ui_options(data)

    st.markdown('<div class="action-title">Choose your move</div>', unsafe_allow_html=True)
    experience_options = ["Quick Mode", "Self Mix"]
    if "experience_mode" not in st.session_state or st.session_state["experience_mode"] not in experience_options:
        st.session_state["experience_mode"] = "Self Mix"
    experience_mode = st.radio(
        "Mix Mode",
        options=experience_options,
        key="experience_mode",
        horizontal=True,
        label_visibility="collapsed",
    )

    if experience_mode == "Quick Mode":
        vibe_options = list(PERSONALITY_PROFILES.keys())
        selected_vibe = st.radio(
            "Vibe Options",
            options=vibe_options,
            key="selected_vibe_option",
            horizontal=True,
        )
        vibe_seed, vibe_description = personality_seed_option(data, selected_vibe)
        st.caption(vibe_description)
        st.success(f"Vibe starter: {vibe_seed}")
        return {vibe_seed: 1.0}, {
            "experience_mode": "Quick Mode",
            "quick_vibe": selected_vibe,
        }

    start_mode_options = ["Pick Songs", "Pick Artists", "Surprise Me"]
    if "start_mode" not in st.session_state or st.session_state["start_mode"] not in start_mode_options:
        st.session_state["start_mode"] = "Pick Songs"
    start_mode = st.radio(
        "How to Start",
        options=start_mode_options,
        key="start_mode",
        horizontal=True,
        label_visibility="collapsed",
    )

    if start_mode == "Pick Songs":
        st.caption("Choose songs (search enabled). Max 5 total picks combined with artists.")
        song_limit = max(0, MAX_SEED_SELECTIONS - len(st.session_state["selected_seed_artists"]))
        if "song_picker_nonce" not in st.session_state:
            st.session_state["song_picker_nonce"] = 0
        song_picker_key = f"song_picker_ui_{st.session_state['song_picker_nonce']}"

        selected_songs = st.multiselect(
            "Song Picks",
            options=song_options,
            key=song_picker_key,
            placeholder="Search and select songs",
            max_selections=max(song_limit, 1),
            disabled=(song_limit == 0),
        )
        if song_limit == 0:
            st.caption("Song picks are locked because all 5 slots are already used by artist picks.")
        elif selected_songs:
            for song_name in _dedupe_preserve_order([str(item) for item in selected_songs]):
                _add_seed_selection("selected_seed_songs", song_name, MAX_SEED_SELECTIONS)
            st.session_state["song_picker_nonce"] += 1
            st.rerun()

        st.caption("Quick top songs")
        clicked_song = render_toggle_chips(
            quick_top_songs,
            selected_values=list(st.session_state["selected_seed_songs"]),
            key_prefix="quick_song_chip",
            min_chips=5,
        )
        if clicked_song:
            _toggle_seed_selection("selected_seed_songs", clicked_song, MAX_SEED_SELECTIONS)

        _render_final_pick_box()
        return _build_seed_weights_from_state(data), {"experience_mode": "Self Mix"}

    if start_mode == "Pick Artists":
        st.caption("Choose artists (search enabled). Max 5 total picks combined with songs.")
        artist_limit = max(0, MAX_SEED_SELECTIONS - len(st.session_state["selected_seed_songs"]))
        if "artist_picker_nonce" not in st.session_state:
            st.session_state["artist_picker_nonce"] = 0
        artist_picker_key = f"artist_picker_ui_{st.session_state['artist_picker_nonce']}"

        selected_artists = st.multiselect(
            "Artist Picks",
            options=artist_options,
            key=artist_picker_key,
            placeholder="Search and select artists",
            max_selections=max(artist_limit, 1),
            disabled=(artist_limit == 0),
        )
        if artist_limit == 0:
            st.caption("Artist picks are locked because all 5 slots are already used by song picks.")
        elif selected_artists:
            for artist_name in _dedupe_preserve_order([str(item) for item in selected_artists]):
                _add_seed_selection("selected_seed_artists", artist_name, MAX_SEED_SELECTIONS)
            st.session_state["artist_picker_nonce"] += 1
            st.rerun()

        st.caption("Quick top artists")
        clicked_artist = render_toggle_chips(
            quick_top_artists,
            selected_values=list(st.session_state["selected_seed_artists"]),
            key_prefix="quick_artist_chip",
            min_chips=5,
        )
        if clicked_artist:
            _toggle_seed_selection("selected_seed_artists", clicked_artist, MAX_SEED_SELECTIONS)

        _render_final_pick_box()
        return _build_seed_weights_from_state(data), {"experience_mode": "Self Mix"}

    if start_mode == "Surprise Me":
        if "surprise_counter" not in st.session_state:
            st.session_state["surprise_counter"] = 0

        if "surprise_seed_track" not in st.session_state:
            st.session_state["surprise_seed_track"] = ""

        if st.button("Spin Random Song", key="surprise_seed_button", use_container_width=False) or not st.session_state[
            "surprise_seed_track"
        ]:
            st.session_state["surprise_counter"] += 1
            candidates = data.sort_values(["momentum_score", "views", "stream"], ascending=False).head(250)
            picked_seed = candidates.sample(1, random_state=9000 + st.session_state["surprise_counter"]).iloc[0]["display_name"]
            st.session_state["surprise_seed_track"] = str(picked_seed)

        surprise_seed = str(st.session_state["surprise_seed_track"])
        st.success(f"Random starter: {surprise_seed}")
        st.caption("This mode uses one random song as the only starting point.")
        return {surprise_seed: 1.0}, {"experience_mode": "Self Mix"}
    return {_default_seed_track(data): 1.0}, {"experience_mode": "Self Mix"}


def main() -> None:
    st.set_page_config(page_title="DJ Mixing Station Studio", page_icon="🎵", layout="wide")
    apply_theme()

    st.markdown(
        """
        <div class="hero">
          <h1>DJ Mixing Station Studio</h1>
          <p>Shape your sound with smart mood controls and cross-platform signal blending.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("Input")
        dataset_id = st.text_input("Kaggle Dataset", value=DEFAULT_DATASET_ID)
        uploaded_file = st.file_uploader("Or upload CSV", type=["csv"])

    try:
        raw_df, source_path, dataset_path = read_source(uploaded_file, dataset_id)
        data = prepare_music_data_cached(raw_df)
        st.session_state["startup_health_status"] = "Healthy"
    except Exception as exc:
        st.session_state["startup_health_status"] = "Unhealthy"
        with st.sidebar:
            st.error("Startup Health: Unhealthy")
        st.error(str(exc))
        st.stop()

    with st.sidebar:
        st.success(f"Startup Health: {st.session_state.get('startup_health_status', 'Unknown')}")

    if dataset_path != "uploaded_file":
        st.caption(f"Path to dataset files: `{dataset_path}`")
        st.caption(f"CSV source: `{source_path}`")

    if data.empty:
        st.error("No usable rows found after cleaning the dataset.")
        st.stop()

    seed_weights, experience_state = render_seed_experience(data)

    with st.sidebar:
        st.header("Recommendation Controls")
        if experience_state.get("experience_mode") == "Quick Mode":
            selected_quick_vibe = str(experience_state.get("quick_vibe", "Focus Flow"))
            quick_defaults = QUICK_VIBE_DEFAULTS.get(selected_quick_vibe, QUICK_VIBE_DEFAULTS["Focus Flow"])
            if st.session_state.get("quick_vibe_applied") != selected_quick_vibe:
                st.session_state["quick_spotify_weight_pct"] = int(quick_defaults["spotify_weight_pct"])
                st.session_state["quick_discovery_hits_pct"] = int(quick_defaults["discovery_hits_pct"])
                st.session_state["quick_vibe_applied"] = selected_quick_vibe

            mood = str(quick_defaults["mood"])
            spotify_weight_pct = st.slider("Platform Bias", 0, 100, key="quick_spotify_weight_pct")
            youtube_weight_pct = 100 - spotify_weight_pct
            st.caption(f"Spotify ({spotify_weight_pct}%) <- -> YouTube ({youtube_weight_pct}%)")
            discovery_hits_pct = st.slider("Discovery Mode", 0, 100, key="quick_discovery_hits_pct")
            hidden_gems_pct = 100 - discovery_hits_pct
            st.caption(f"Hits ({discovery_hits_pct}%) <- -> Hidden Gems ({hidden_gems_pct}%)")
        else:
            self_mix_vibe = st.selectbox("Vibe Options", options=list(PERSONALITY_PROFILES.keys()), index=0, key="self_mix_vibe_option")
            mood = str(QUICK_VIBE_DEFAULTS.get(self_mix_vibe, QUICK_VIBE_DEFAULTS["Focus Flow"])["mood"])
            if "spotify_weight_pct" not in st.session_state:
                st.session_state["spotify_weight_pct"] = 65
            spotify_weight_pct = st.slider("Platform Bias", 0, 100, key="spotify_weight_pct")
            youtube_weight_pct = 100 - spotify_weight_pct
            st.caption(f"Spotify ({spotify_weight_pct}%) <- -> YouTube ({youtube_weight_pct}%)")
            if "discovery_hits_pct" not in st.session_state:
                st.session_state["discovery_hits_pct"] = 65
            discovery_hits_pct = st.slider("Discovery Mode", 0, 100, key="discovery_hits_pct")
            hidden_gems_pct = 100 - discovery_hits_pct
            st.caption(f"Hits ({discovery_hits_pct}%) <- -> Hidden Gems ({hidden_gems_pct}%)")

        target_minutes = st.slider("Total Playlist Minutes (±3 mins)", 30, 300, 120)
        lower_window = target_minutes - PLAYLIST_TOLERANCE_MINUTES
        upper_window = target_minutes + PLAYLIST_TOLERANCE_MINUTES
        st.caption(
            f"Playlist window: {lower_window} to {upper_window} mins "
            f"({format_hours_minutes(lower_window)} to {format_hours_minutes(upper_window)})"
        )

    seed_weight_items = tuple(sorted((str(name), float(weight)) for name, weight in seed_weights.items()))
    preferred_artist_weight_items: tuple[tuple[str, float], ...] = ()
    if experience_state.get("experience_mode") == "Self Mix":
        preferred_artist_weight_items = tuple(sorted(_build_preferred_artist_weights(data).items()))
    include_seed_tracks = discovery_hits_pct == 100

    recommendations = recommend_tracks_cached(
        data=data,
        seed_weight_items=seed_weight_items,
        mood=mood,
        spotify_weight=spotify_weight_pct / 100.0,
        discovery_mode=hidden_gems_pct / 100.0,
        top_k=180,
        exclude_seed_tracks=not include_seed_tracks,
        preferred_artist_weight_items=preferred_artist_weight_items,
    )
    playlist = build_duration_playlist_cached(
        recommendations=recommendations,
        target_minutes=target_minutes,
        tolerance_minutes=PLAYLIST_TOLERANCE_MINUTES,
        candidate_limit=180,
        max_tracks=50,
    )
    playlist["mood_tag"] = mood
    total_playlist_minutes = pd.to_numeric(playlist.get("duration_ms"), errors="coerce").fillna(0).sum() / 60_000
    left, mid, right = st.columns(3)
    with left:
        render_metric_card("Tracks Available", f"{len(data):,}")
    with mid:
        render_metric_card("Artists Available", f"{data['artist'].nunique():,}")
    with right:
        render_metric_card("Playlist Duration", format_total_duration(total_playlist_minutes))

    if playlist.empty:
        st.warning("No playlist could be generated for this duration target. Try different seed selections.")
        st.stop()
    if not (lower_window <= total_playlist_minutes <= upper_window):
        st.warning(
            f"No exact playlist found in {lower_window}-{upper_window} mins. "
            f"Showing closest match: {format_total_duration(total_playlist_minutes)}."
        )

    st.subheader("Playable Playlist")
    queue = playlist.copy().reset_index(drop=True)
    queue["position"] = queue.index + 1
    queue["duration_text"] = queue["duration_ms"].apply(format_track_duration)
    queue["spotify_url"] = queue.apply(
        lambda row: row.get("spotify_link") or row.get("url_spotify") or "",
        axis=1,
    )
    queue["youtube_url"] = queue.apply(
        lambda row: prefer_direct_youtube_url(row.get("youtube_link"), row.get("url_youtube")),
        axis=1,
    )
    queue["queue_label"] = queue.apply(
        lambda row: f'{int(row["position"]):02d}. {row["artist"]} - {row["track"]} ({row["duration_text"]})',
        axis=1,
    )

    player_col, mode_col = st.columns([3, 2])
    with player_col:
        selected_label = st.selectbox("Now Playing", options=queue["queue_label"].tolist())
    with mode_col:
        playback_platform = st.radio("Playback", options=["Auto", "Spotify", "YouTube"], horizontal=True)

    selected_row = queue.loc[queue["queue_label"] == selected_label].iloc[0]
    selected_spotify = str(selected_row["spotify_url"]).strip()
    selected_youtube, selected_youtube_watch = resolve_playback_youtube_targets(selected_row)
    selected_youtube = str(selected_youtube).strip()
    selected_youtube_watch = str(selected_youtube_watch).strip()
    selected_spotify_track_id = extract_spotify_track_id(selected_spotify)
    youtube_embed_blocked = False

    # If embed target looks unavailable, force one live resolver attempt before rendering player.
    if (playback_platform in {"YouTube", "Auto"}) and not selected_youtube_watch:
        forced_link, forced_embed = resolve_playback_youtube_targets(
            selected_row,
            force_live=True,
            live_timeout=4,
        )
        forced_link = str(forced_link).strip()
        forced_embed = str(forced_embed).strip()
        if forced_link:
            selected_youtube = forced_link
        if forced_embed:
            selected_youtube_watch = forced_embed

    if selected_youtube_watch and playback_platform in {"YouTube", "Auto"}:
        try:
            if not _is_youtube_embed_likely_available(selected_youtube_watch):
                youtube_embed_blocked = True
                selected_youtube_watch = ""
        except Exception:
            # Keep playback resilient even if the embed probe fails.
            pass

    st.markdown(
        f'**{selected_row["artist"]} - {selected_row["track"]}** · {selected_row["duration_text"]} '
        f'· Momentum {selected_row["momentum_score"]:.3f}'
    )
    render_track_links(selected_spotify, selected_youtube)

    def _render_spotify_embed(track_id: str) -> None:
        embed_url = f"https://open.spotify.com/embed/track/{track_id}?utm_source=generator"
        components.html(
            f'<iframe src="{embed_url}" width="100%" height="152" frameborder="0" '
            'allowfullscreen="" allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"></iframe>',
            height=170,
        )

    if playback_platform == "YouTube":
        if selected_youtube_watch:
            st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            if youtube_embed_blocked:
                st.info("This YouTube video can't be embedded here. Falling back to Spotify player.")
            else:
                st.info("YouTube embed unavailable for this track. Falling back to Spotify player.")
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_youtube:
            if youtube_embed_blocked:
                st.info("This YouTube video can't be embedded here. Use the YouTube button.")
            else:
                st.info("No direct YouTube video ID available for embed. Use the YouTube button.")
    elif playback_platform == "Spotify":
        if selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)
        elif selected_spotify:
            st.info("No direct Spotify track ID available for embed. Use the Spotify button.")
    else:
        if selected_youtube_watch:
            st.video(selected_youtube_watch)
        elif selected_spotify_track_id:
            _render_spotify_embed(selected_spotify_track_id)

    with st.spinner("Building temporary YouTube playlist from playable URLs..."):
        youtube_ids_result = build_playable_youtube_ids(
            queue,
            max_ids=50,
            max_live_resolves=16,
            live_timeout=3,
            min_ids_required=2,
            fill_to_max=False,
            max_total_seconds=10.0,
            return_stats=True,
        )
    if isinstance(youtube_ids_result, tuple):
        youtube_ids, yt_stats = youtube_ids_result
    else:
        youtube_ids = youtube_ids_result
        yt_stats = {
            "playable_count": len(youtube_ids),
            "playable_linked_rows": len(youtube_ids),
            "target_rows": min(len(queue), 50),
            "included_direct": 0,
            "included_resolved": 0,
            "resolver_attempted": 0,
            "resolver_resolved": 0,
            "duplicate_rows": 0,
            "unresolved_rows": 0,
            "budget_blocked_rows": 0,
            "resolve_budget": 0,
            "row_diagnostics": [],
        }

    if len(youtube_ids) >= 2:
        temp_youtube_playlist = "https://www.youtube.com/watch_videos?video_ids=" + ",".join(youtube_ids[:50])
        render_platform_link("Open Temporary YouTube Playlist", temp_youtube_playlist)
    else:
        st.caption("Temporary YouTube playlist link requires at least 2 playable YouTube IDs.")
    target_rows = int(yt_stats.get("target_rows", 0) or 0)
    playable_rows = int(yt_stats.get("playable_count", 0) or 0)
    linked_rows = int(yt_stats.get("playable_linked_rows", playable_rows) or playable_rows)
    if target_rows > 0:
        coverage_pct = (linked_rows / target_rows) * 100.0
        duplicate_collapsed = max(0, linked_rows - playable_rows)
        st.caption(
            "Temporary playlist coverage: "
            f"{linked_rows}/{target_rows} rows linked ({coverage_pct:.0f}%). "
            f"Unique YouTube IDs: {playable_rows}. "
            f"Direct: {int(yt_stats.get('included_direct', 0) or 0)} · "
            f"Resolved: {int(yt_stats.get('included_resolved', 0) or 0)} · "
            f"Resolver attempts: {int(yt_stats.get('resolver_attempted', 0) or 0)}."
        )
        if duplicate_collapsed > 0:
            st.caption(f"Duplicate IDs collapsed: {duplicate_collapsed}")
    st.session_state["_yt_playlist_row_diagnostics"] = (
        yt_stats.get("row_diagnostics", []) if isinstance(yt_stats.get("row_diagnostics", []), list) else []
    )

    st.subheader("Recommendation Debug View")
    render_modern_debug_table(playlist)


if __name__ == "__main__":
    main()
