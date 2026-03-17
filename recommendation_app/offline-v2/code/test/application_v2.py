from __future__ import annotations

import re
import sys
import traceback
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd

MODE_DIR = Path(__file__).resolve().parent
CODE_ROOT = MODE_DIR.parent
if str(MODE_DIR) not in sys.path:
    sys.path.insert(0, str(MODE_DIR))
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(1, str(CODE_ROOT))

import runtime_config

import app as base_app  # type: ignore

try:
    from recommendation_app.data_upgrade_v2 import (
        BASELINE_DATASET_ID,
        EXPANSION_DATASET_ID,
        load_and_merge_from_kagglehub,
    )
    from recommendation_app.recommender_v2_adapter import (
        build_duration_playlist,
        prepare_music_data,
        recommend_tracks,
    )
except ModuleNotFoundError:
    from data_upgrade_v2 import BASELINE_DATASET_ID, EXPANSION_DATASET_ID, load_and_merge_from_kagglehub  # type: ignore
    from recommender_v2_adapter import build_duration_playlist, prepare_music_data, recommend_tracks  # type: ignore

try:
    import recommendation_app.youtube_live_resolver as yt_resolver  # type: ignore
except ModuleNotFoundError:
    try:
        import youtube_live_resolver as yt_resolver  # type: ignore
    except ModuleNotFoundError:
        yt_resolver = None  # type: ignore[assignment]


_ORIGINAL_RENDER_TRACK_LINKS = base_app.render_track_links
_YT_META_BY_URL: dict[str, dict[str, str]] = {}


def _parse_dual_dataset_input(dataset_id: str) -> tuple[str, str]:
    text = str(dataset_id or "").strip()
    if not text:
        return BASELINE_DATASET_ID, EXPANSION_DATASET_ID

    parts = [part.strip() for part in text.split("+") if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]

    # If user gives one dataset id, keep it as baseline and use expansion default.
    return parts[0], EXPANSION_DATASET_ID


@base_app.st.cache_data(show_spinner=False)
def load_merged_from_kagglehub_cached(baseline_dataset_id: str, expansion_dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    return load_and_merge_from_kagglehub(
        baseline_dataset_id=baseline_dataset_id,
        expansion_dataset_id=expansion_dataset_id,
    )


def read_source_v2(uploaded_file: Any, dataset_id: str) -> tuple[pd.DataFrame, str, str]:
    if uploaded_file is not None:
        return pd.read_csv(uploaded_file), uploaded_file.name, "uploaded_file"

    baseline_id, expansion_id = _parse_dual_dataset_input(dataset_id)
    return load_merged_from_kagglehub_cached(baseline_id, expansion_id)


@base_app.st.cache_data(show_spinner=False)
def _prepare_music_data_cached_v2_core(raw_df: pd.DataFrame) -> pd.DataFrame:
    return prepare_music_data(raw_df)


def prepare_music_data_cached_v2(raw_df: pd.DataFrame) -> pd.DataFrame:
    # Always refresh runtime metadata, even when data rows come from Streamlit cache.
    prepared = _prepare_music_data_cached_v2_core(raw_df)
    _refresh_youtube_meta(prepared)
    return prepared


@base_app.st.cache_data(show_spinner=False)
def recommend_tracks_cached_v2(
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


@base_app.st.cache_data(show_spinner=False)
def build_duration_playlist_cached_v2(
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


def _youtube_confidence_label(score: float) -> str:
    if score >= 7.0:
        return "High"
    if score >= 5.0:
        return "Medium"
    return "Low"


def _safe_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y"}


def _refresh_youtube_meta(data: pd.DataFrame) -> None:
    _YT_META_BY_URL.clear()
    if data.empty:
        return

    url_col = data.get("youtube_link", pd.Series([""] * len(data), index=data.index)).fillna("").astype(str)
    fallback_col = data.get("url_youtube", pd.Series([""] * len(data), index=data.index)).fillna("").astype(str)
    score_col = pd.to_numeric(data.get("youtube_quality_score", 0), errors="coerce").fillna(0.0)
    official_mv_col = data.get("yt_title_official_mv", False)
    official_audio_col = data.get("yt_title_official_audio", False)
    channel_match_col = data.get("yt_channel_artist_match", 0)
    views_col = pd.to_numeric(data.get("views", 0), errors="coerce").fillna(0.0)

    for idx in data.index:
        watch_url = base_app.normalize_youtube_watch_url(url_col.loc[idx]) or base_app.normalize_youtube_watch_url(fallback_col.loc[idx])
        if not watch_url:
            continue

        score = float(score_col.loc[idx])
        reasons: list[str] = []
        if _safe_bool(official_mv_col.loc[idx] if isinstance(official_mv_col, pd.Series) else official_mv_col):
            reasons.append("Official Music Video")
        elif _safe_bool(official_audio_col.loc[idx] if isinstance(official_audio_col, pd.Series) else official_audio_col):
            reasons.append("Official Audio")

        channel_match_value = channel_match_col.loc[idx] if isinstance(channel_match_col, pd.Series) else channel_match_col
        try:
            if float(channel_match_value) > 0:
                reasons.append("Artist channel match")
        except (TypeError, ValueError):
            pass

        views_value = float(views_col.loc[idx])
        reasons.append(f"{base_app.format_compact_number(views_value)} views")

        candidate = {
            "confidence": _youtube_confidence_label(score),
            "score": f"{score:.2f}",
            "reason": " · ".join(reasons),
        }

        current = _YT_META_BY_URL.get(watch_url)
        if current is None or float(candidate["score"]) > float(current["score"]):
            _YT_META_BY_URL[watch_url] = candidate


def render_track_links_v2(spotify_url: object, youtube_url: object) -> None:
    _ORIGINAL_RENDER_TRACK_LINKS(spotify_url, youtube_url)
    # Keep player area clean for end users. Confidence stays in Debug View rows.
    return


def _format_cache_expiry(expires_at: object) -> str:
    text = str(expires_at or "").strip()
    if not text:
        return "-"
    try:
        expiry_dt = datetime.fromisoformat(text)
    except Exception:
        return "-"

    if expiry_dt.tzinfo is None:
        expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)

    remaining = expiry_dt - datetime.now(timezone.utc)
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "Expired"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{max(1, minutes)}m"


def _resolver_cache_status(artist: object, track: object, has_direct_video: bool) -> tuple[str, str]:
    if has_direct_video:
        return "No", "-"
    if yt_resolver is None:
        return "No", "-"
    if not hasattr(yt_resolver, "_cache_key") or not hasattr(yt_resolver, "_cache_get"):
        return "No", "-"

    try:
        cache_key = yt_resolver._cache_key(str(artist or ""), str(track or ""))
        cache_entry = yt_resolver._cache_get(cache_key)
    except Exception:
        return "No", "-"

    if not cache_entry:
        return "No", "-"

    cache_watch = str(cache_entry.get("watch_url", "") or "").strip()
    if not cache_watch:
        return "No", "-"

    return "Yes", _format_cache_expiry(cache_entry.get("expires_at"))


def render_modern_debug_table_v2(playlist: pd.DataFrame) -> None:
    if playlist.empty:
        base_app.st.caption("No debug rows to display.")
        return

    safe_playlist = playlist.copy()
    for numeric_col in ("recommendation_score", "similarity_score", "platform_score", "discovery_score", "momentum_score"):
        safe_playlist[numeric_col] = pd.to_numeric(safe_playlist[numeric_col], errors="coerce").fillna(0.0)

    def score_cell(value: float) -> str:
        clipped = max(0.0, min(1.0, float(value)))
        pct = clipped * 100.0
        return (
            '<td class="debug-score-cell"><div class="debug-score-wrap">'
            f'<div class="debug-score-track"><div class="debug-score-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="debug-score-value">{clipped:.3f}</span>'
            "</div></td>"
        )

    def _first_nonempty_text(*values: object) -> str:
        for value in values:
            if value is None:
                continue
            if isinstance(value, float) and pd.isna(value):
                continue
            text = str(value).strip()
            if text and text.lower() != "nan":
                return text
        return ""

    def _row_confidence_label(row: pd.Series, watch_url: str) -> str:
        row_score = pd.to_numeric(pd.Series([row.get("youtube_quality_score", float("nan"))]), errors="coerce").iloc[0]
        if pd.notna(row_score):
            return f"{_youtube_confidence_label(float(row_score))} ({float(row_score):.2f})"
        meta = _YT_META_BY_URL.get(watch_url or "", {})
        if meta:
            return f"{meta.get('confidence', 'Low')} ({meta.get('score', '0.00')})"
        return "Low (0.00)"

    rows_html: list[str] = []
    cache_meta_by_track: dict[tuple[str, str, bool], tuple[str, str]] = {}
    row_diagnostics = base_app.st.session_state.get("_yt_playlist_row_diagnostics", [])
    for idx, row in safe_playlist.reset_index(drop=True).iterrows():
        youtube_ref = _first_nonempty_text(row.get("youtube_link"), row.get("url_youtube"))
        watch_url = base_app.normalize_youtube_watch_url(youtube_ref)
        direct_video_id = base_app.extract_youtube_video_id(youtube_ref)
        direct_video_label = "Yes" if direct_video_id else "No"
        cache_key = (str(row.get("artist", "")), str(row.get("track", "")), bool(direct_video_id))
        if cache_key not in cache_meta_by_track:
            cache_meta_by_track[cache_key] = _resolver_cache_status(
                artist=row.get("artist", ""),
                track=row.get("track", ""),
                has_direct_video=bool(direct_video_id),
            )
        cache_source_label, cache_expiry_label = cache_meta_by_track[cache_key]
        confidence_label = _row_confidence_label(row, watch_url or "")
        diag = row_diagnostics[idx] if isinstance(row_diagnostics, list) and idx < len(row_diagnostics) else {}
        playlist_status = str(diag.get("playlist_status", "-"))
        playlist_source = str(diag.get("playlist_source", "-"))
        skip_reason = str(diag.get("skip_reason", "-"))
        skip_detail = str(diag.get("skip_detail", "-"))
        youtube_id = str(diag.get("youtube_id", "-"))

        rows_html.append(
            "<tr>"
            f'<td class="debug-rank">{idx + 1}</td>'
            f'<td class="debug-subtle">{escape(str(row.get("artist", "")))}</td>'
            f'<td class="debug-track">{escape(str(row.get("track", "")))}</td>'
            f'<td class="debug-subtle">{escape(direct_video_label)}</td>'
            f'<td class="debug-subtle">{escape(cache_source_label)}</td>'
            f'<td class="debug-subtle">{escape(cache_expiry_label)}</td>'
            f'<td class="debug-subtle">{escape(confidence_label)}</td>'
            f'<td class="debug-subtle">{escape(playlist_status)}</td>'
            f'<td class="debug-subtle">{escape(playlist_source)}</td>'
            f'<td class="debug-subtle">{escape(skip_reason)}</td>'
            f'<td class="debug-subtle">{escape(skip_detail)}</td>'
            f'<td class="debug-subtle">{escape(youtube_id)}</td>'
            f'<td class="debug-subtle">{escape(base_app.format_track_duration(row.get("duration_ms", 0)))}</td>'
            f"{score_cell(float(row.get('recommendation_score', 0.0)))}"
            f"{score_cell(float(row.get('momentum_score', 0.0)))}"
            f'<td class="debug-subtle">{escape(base_app.format_compact_number(float(row.get("views", 0) or 0)))}</td>'
            f'<td class="debug-subtle">{escape(base_app.format_compact_number(float(row.get("stream", 0) or 0)))}</td>'
            "</tr>"
        )

    base_app.st.markdown(
        (
            '<div class="modern-debug-wrap"><table class="modern-debug-table">'
            "<thead><tr>"
            "<th>Rank</th><th>Artist</th><th>Track</th><th>Direct YouTube</th><th>YouTube Resolve Cache</th><th>Cache Expires In</th><th>YouTube Confidence</th><th>Playlist Link</th><th>Link Source</th><th>Skip Reason</th><th>Skip Detail</th><th>YouTube ID</th><th>Duration</th><th>Match</th><th>Momentum</th><th>Views</th><th>Streams</th>"
            "</tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody></table></div>"
        ),
        unsafe_allow_html=True,
    )


def _artist_options_with_credits(data: pd.DataFrame) -> list[str]:
    artist_names = set(data["artist"].astype(str).tolist())

    if "artist_credit_norms" in data.columns:
        # Keep display form from artist_credits JSON when possible, fallback to normalized names.
        for credit_blob in data["artist_credits"].fillna("").astype(str):
            if not credit_blob.strip():
                continue
            try:
                import json

                parsed = json.loads(credit_blob)
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip()
                        if name:
                            artist_names.add(name)

    return sorted(name for name in artist_names if str(name).strip())


@base_app.st.cache_data(show_spinner=False)
def get_seed_ui_options_v2(data: pd.DataFrame) -> tuple[list[str], list[str], list[str], list[str]]:
    song_options = sorted(data["display_name"].astype(str).unique().tolist())
    artist_options = _artist_options_with_credits(data)
    quick_top_songs = base_app.top_song_seed_options(data, top_n=8)
    quick_top_artists = base_app.top_artist_seed_options(data, top_n=8)["artist"].astype(str).tolist()
    return song_options, artist_options, quick_top_songs, quick_top_artists


def best_track_for_artist_v2(data: pd.DataFrame, artist_name: str) -> str | None:
    target = str(artist_name).strip()
    if not target:
        return None

    artist_rows = data.loc[data["artist"] == target].copy()

    target_key = re.sub(r"[^a-z0-9 ]+", " ", target.lower()).strip()

    featured_rows = data.head(0).copy()
    if target_key and "featured_artist_norms" in data.columns:
        featured_mask = data["featured_artist_norms"].fillna("").astype(str).str.contains(
            rf"(?:^|\|){re.escape(target_key)}(?:\||$)",
            regex=True,
        )
        featured_rows = data.loc[featured_mask].copy()

    credit_rows = data.head(0).copy()
    if target_key and "artist_credit_norms" in data.columns:
        credit_mask = data["artist_credit_norms"].fillna("").astype(str).str.contains(
            rf"(?:^|\|){re.escape(target_key)}(?:\||$)",
            regex=True,
        )
        credit_rows = data.loc[credit_mask].copy()

    candidate_rows = pd.concat([artist_rows, featured_rows, credit_rows], ignore_index=False)
    candidate_rows = candidate_rows.drop_duplicates(subset=["display_name"], keep="first")
    if candidate_rows.empty:
        return None

    best = candidate_rows.sort_values(["momentum_score", "views", "stream"], ascending=False).iloc[0]
    return str(best["display_name"])


def patch_base_app_for_v2() -> None:
    base_app.DEFAULT_DATASET_ID = f"{BASELINE_DATASET_ID} + {EXPANSION_DATASET_ID}"
    base_app.read_source = read_source_v2
    base_app.prepare_music_data_cached = prepare_music_data_cached_v2
    base_app.recommend_tracks_cached = recommend_tracks_cached_v2
    base_app.build_duration_playlist_cached = build_duration_playlist_cached_v2
    base_app.get_seed_ui_options = get_seed_ui_options_v2
    base_app.best_track_for_artist = best_track_for_artist_v2
    base_app.render_track_links = render_track_links_v2
    base_app.render_modern_debug_table = render_modern_debug_table_v2


if __name__ == "__main__":
    patch_base_app_for_v2()
    try:
        base_app.main()
    except Exception as exc:
        # Safe UI error boundary for production-like mode.
        base_app.st.error("Something went wrong while generating your playlist. Please retry.")
        if runtime_config.DEBUG:
            base_app.st.exception(exc)
        else:
            traceback.print_exc()
