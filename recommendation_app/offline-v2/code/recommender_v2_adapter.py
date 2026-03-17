from __future__ import annotations

import json
import re
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

import pandas as pd

try:
    from recommendation_app.recommender_v2_core import (  # type: ignore
        FEATURE_COLUMNS,
        MOOD_ADJUSTMENTS,
        REQUIRED_COLUMNS,
        build_duration_playlist,
        format_compact_number,
        prepare_music_data as _prepare_music_data_base,
        recommend_tracks,
    )
except ModuleNotFoundError:
    from recommender_v2_core import (  # type: ignore
        FEATURE_COLUMNS,
        MOOD_ADJUSTMENTS,
        REQUIRED_COLUMNS,
        build_duration_playlist,
        format_compact_number,
        prepare_music_data as _prepare_music_data_base,
        recommend_tracks,
    )


def _clean_text(value: object) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(value).lower()).strip()


def _extract_youtube_video_id(url: object) -> str | None:
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


def _parse_artist_credits(value: object) -> tuple[set[str], set[str]]:
    text = str(value or "").strip()
    if not text:
        return set(), set()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []

    all_names: set[str] = set()
    featured_names: set[str] = set()

    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name", ""))
            role = str(item.get("role", "featured")).strip().lower()
            if not name:
                continue
            all_names.add(name)
            if role == "featured":
                featured_names.add(name)

    return all_names, featured_names


def prepare_music_data(df: pd.DataFrame) -> pd.DataFrame:
    raw = df.copy()
    raw.columns = [str(c).strip().lower() for c in raw.columns]

    if "artist_owner" in raw.columns:
        owner_series = raw["artist_owner"].fillna("").astype(str).str.strip()
        if "artist" in raw.columns:
            artist_series = raw["artist"].fillna("").astype(str).str.strip()
            raw["artist"] = owner_series.where(owner_series != "", artist_series)
        else:
            raw["artist"] = owner_series

    prepared = _prepare_music_data_base(raw)

    if "artist_credits" not in prepared.columns:
        prepared["artist_credits"] = ""

    extra_featured_norms: List[str] = []
    all_credit_norms: List[str] = []

    for _, row in prepared.iterrows():
        owner_norm = _clean_text(row.get("artist", ""))
        all_norms, explicit_featured_norms = _parse_artist_credits(row.get("artist_credits", ""))

        # Non-owner collaborators are treated as feature-match eligible.
        collaborator_norms = {name for name in all_norms if name and name != owner_norm}

        existing_featured = set(str(row.get("featured_artist_norms", "")).split("|")) if row.get("featured_artist_norms") else set()
        existing_featured = {name for name in existing_featured if name}

        combined_featured = sorted(existing_featured | explicit_featured_norms | collaborator_norms)
        combined_all = sorted({name for name in all_norms if name})

        extra_featured_norms.append("|".join(combined_featured))
        all_credit_norms.append("|".join(combined_all))

    prepared["featured_artist_norms"] = extra_featured_norms
    prepared["artist_credit_norms"] = all_credit_norms

    # Keep embed-ready links whenever a direct YouTube video ID exists.
    if "url_youtube" in prepared.columns:
        yids = prepared["url_youtube"].apply(_extract_youtube_video_id)
        has_id = yids.fillna("").astype(str).str.len() > 0
        prepared.loc[has_id, "youtube_link"] = yids[has_id].apply(lambda value: f"https://www.youtube.com/watch?v={value}")

    return prepared
