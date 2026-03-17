from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, validate_prepared_dataset

if str(cfg.OFFLINE_V2_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(cfg.OFFLINE_V2_CODE_DIR))
OFFLINE_V2_MODE_DIR = cfg.OFFLINE_V2_CODE_DIR / "test"
if str(OFFLINE_V2_MODE_DIR) not in sys.path:
    sys.path.insert(0, str(OFFLINE_V2_MODE_DIR))

import application_v2 as app_v2  # type: ignore  # noqa: E402

try:
    import youtube_live_resolver as yt_resolver  # type: ignore
except ModuleNotFoundError:
    yt_resolver = None  # type: ignore[assignment]


base_app = app_v2.base_app
_ORIGINAL_RESOLVE_LIVE_YOUTUBE = base_app.resolve_live_youtube_watch_url
_PLAYLIST_PLAYABLE_CACHE: dict[str, bool] = {}


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


def _normalize_text(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_feature_text(track_name: str) -> str:
    text = str(track_name or "").strip()
    text = re.sub(r"\s*\((?:feat\.?|ft\.?).*?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*(?:feat\.?|ft\.?)\s+.+$", "", text, flags=re.IGNORECASE)
    return text.strip()


def _is_live_requested(track_name: str) -> bool:
    norm = _normalize_text(track_name)
    return bool(re.search(r"\blive\b|\bao vivo\b|\ben vivo\b|\bconcert\b", norm))


def _candidate_adjustment(track_name: str, artist_owner: str, candidate: dict[str, Any]) -> float:
    title = str(candidate.get("title", "") or "")
    channel = str(candidate.get("channel_title", "") or "")
    title_norm = _normalize_text(title)
    channel_norm = _normalize_text(channel)
    owner_norm = _normalize_text(artist_owner)
    track_core_norm = _normalize_text(_strip_feature_text(track_name) or track_name)
    request_wants_live = _is_live_requested(track_name)

    adjustment = 0.0

    has_live_signal = bool(
        re.search(r"\blive\b|\bsnl\b|\bsaturday night live\b|\bconcert\b|\btour\b|\blive from\b", title_norm)
    )
    if has_live_signal and not request_wants_live:
        adjustment -= 6.0

    has_medley_signal = (
        "/" in title
        or " x " in f" {title_norm} "
        or " & " in title
        or bool(re.search(r"\b(3d concert|concert experience|entire performance|full performance)\b", title_norm))
    )
    if has_medley_signal and not request_wants_live:
        adjustment -= 3.0

    if track_core_norm:
        if title_norm == track_core_norm:
            adjustment += 5.0
        elif title_norm.startswith(f"{track_core_norm} "):
            adjustment += 2.5
        elif re.search(rf"(^|\s){re.escape(track_core_norm)}(\s|$)", title_norm):
            adjustment += 1.0

        track_tokens = [token for token in track_core_norm.split() if len(token) >= 3]
        title_tokens = set(title_norm.split())
        if track_tokens and not (track_core_norm in title_norm):
            coverage = sum(1 for token in track_tokens if token in title_tokens) / max(1, len(track_tokens))
            if coverage <= 0.0:
                adjustment -= 10.0
            elif coverage < 0.6:
                adjustment -= 6.0

    has_official_channel_signal = bool(re.search(r"\b(official|vevo|topic)\b", channel_norm))
    if owner_norm and owner_norm in channel_norm:
        adjustment += 1.2
    elif has_official_channel_signal:
        adjustment += 0.5
    elif channel_norm:
        # Third-party channels are acceptable fallback only when stronger
        # artist/official signals are unavailable.
        adjustment -= 0.8

    request_norm = _normalize_text(track_name)
    if "feat" not in request_norm and " ft " not in f" {request_norm} ":
        if re.search(r"\b(feat|ft)\b", title_norm):
            adjustment -= 2.5

    if "audio" not in request_norm and re.search(r"\baudio\b", title_norm):
        adjustment -= 1.5

    if re.search(r"\b(fan|lyrics|karaoke|nightcore|sped up|slowed|reaction|exclusive|exclusives)\b", channel_norm):
        adjustment -= 2.0

    return adjustment


def _choose_reranked_candidate(
    track_name: str,
    artist_owner: str,
    candidates: list[dict[str, Any]],
    live_timeout: int = 6,
) -> dict[str, Any] | None:
    if not candidates:
        return None

    ranked: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        base_score = float(candidate.get("score", 0.0) or 0.0)
        adjusted = base_score + _candidate_adjustment(track_name, artist_owner, candidate)
        candidate["_phase2_adjusted_score"] = adjusted
        ranked.append((adjusted, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)

    probe_limit = min(5, len(ranked))
    playable_ranked: list[tuple[float, dict[str, Any]]] = []
    for adjusted, candidate in ranked[:probe_limit]:
        best_watch = str(candidate.get("watch_url", "") or "")
        if not best_watch:
            best_video_id = str(candidate.get("video_id", "") or "")
            if best_video_id:
                best_watch = f"https://www.youtube.com/watch?v={best_video_id}"
        video_id = base_app.extract_youtube_video_id(best_watch)
        if not video_id:
            continue
        if _is_video_playlist_playable_phase2(str(video_id), timeout=max(3, int(live_timeout))):
            playable_ranked.append((adjusted, candidate))

    if playable_ranked:
        playable_ranked.sort(key=lambda item: item[0], reverse=True)
        return playable_ranked[0][1]

    return ranked[0][1]


def _should_refresh_cached_result(track_name: str, reason: str) -> bool:
    if _is_live_requested(track_name):
        return False
    reason_norm = _normalize_text(reason)
    return bool(re.search(r"\blive\b|\bsnl\b|\bsaturday night live\b", reason_norm) or "/" in reason)


def resolve_live_youtube_watch_url_phase2(
    artist: str,
    track: str,
    artist_credits_json: str = "",
    timeout: int = 8,
) -> tuple[str, str, float]:
    if yt_resolver is None:
        return _ORIGINAL_RESOLVE_LIVE_YOUTUBE(artist, track, artist_credits_json, timeout)

    result = yt_resolver.resolve_best_youtube_live(  # type: ignore[union-attr]
        artist_owner=artist,
        track=track,
        artist_credits_json=artist_credits_json,
        max_results=10,
        timeout=timeout,
    )

    watch_url = str(result.get("watch_url", "") or "")
    reason = str(result.get("reason", "") or "")
    score = float(result.get("score", 0.0) or 0.0)
    cache_hit = bool(result.get("cache_hit", False))
    candidates = result.get("candidates", [])

    if cache_hit and _should_refresh_cached_result(track, reason):
        try:
            cache_key = yt_resolver._cache_key(str(artist or ""), str(track or ""))  # type: ignore[union-attr]
            conn = yt_resolver._cache_connect()  # type: ignore[union-attr]
            conn.execute("DELETE FROM youtube_resolver_cache WHERE yt_key = ?", (cache_key,))
            conn.commit()
            conn.close()
            result = yt_resolver.resolve_best_youtube_live(  # type: ignore[union-attr]
                artist_owner=artist,
                track=track,
                artist_credits_json=artist_credits_json,
                max_results=10,
                timeout=timeout,
            )
            watch_url = str(result.get("watch_url", "") or "")
            reason = str(result.get("reason", "") or "")
            score = float(result.get("score", 0.0) or 0.0)
            candidates = result.get("candidates", [])
        except Exception:
            pass

    if isinstance(candidates, list) and candidates:
        best = _choose_reranked_candidate(
            track,
            artist,
            [c for c in candidates if isinstance(c, dict)],
            live_timeout=timeout,
        )
        if best is not None:
            best_watch = str(best.get("watch_url", "") or "")
            if not best_watch:
                best_video_id = str(best.get("video_id", "") or "")
                if best_video_id:
                    best_watch = f"https://www.youtube.com/watch?v={best_video_id}"
            if best_watch:
                watch_url = best_watch
            score = float(best.get("score", score) or score) + _candidate_adjustment(track, artist, best)
            reason = (
                f"reranked resolver candidate: {best.get('title', '')} | "
                f"{best.get('channel_title', '')}"
            )

    return watch_url, reason, score


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _should_force_live_for_direct(row: pd.Series, direct_watch: str) -> bool:
    if not direct_watch:
        return True

    direct_video_id = base_app.extract_youtube_video_id(direct_watch)
    if direct_video_id and not _is_video_playlist_playable_phase2(direct_video_id):
        # Example: copyright-claimed or removed videos.
        return True

    title_norm = _normalize_text(row.get("title", ""))
    channel_match = _as_float(row.get("yt_channel_artist_match"), 0.0)
    official_mv = _as_bool(row.get("yt_title_official_mv"))
    official_audio = _as_bool(row.get("yt_title_official_audio"))
    official_keyword = "official" in title_norm
    has_verified_signal = official_mv or official_audio or channel_match > 0.0

    # Direct links with only title-level "official" text but no structured
    # channel/title trust signals are treated as weak and re-resolved.
    if not has_verified_signal and official_keyword:
        return True

    request_track = str(row.get("track", "") or "")
    if not _is_live_requested(request_track):
        direct_live_signal = bool(
            re.search(
                r"\blive\b|\bsnl\b|\bsaturday night live\b|\bconcert\b|\btour\b|\blive from\b",
                title_norm,
            )
        )
        if direct_live_signal:
            return True
    return False


def resolve_playback_youtube_targets_phase2(
    row: pd.Series,
    force_live: bool = False,
    live_timeout: int = 8,
) -> tuple[str, str]:
    direct_watch = base_app.normalize_youtube_watch_url(row.get("youtube_link")) or base_app.normalize_youtube_watch_url(
        row.get("url_youtube")
    )
    search_fallback = base_app.build_youtube_search_url(
        row.get("artist", ""),
        row.get("track", ""),
        row.get("artist_credits", ""),
    )

    # For single-track playback, trust the current dataset's direct watch URL.
    # Resolver is only needed when no direct link exists or when explicitly forced.
    if direct_watch and not force_live:
        return direct_watch, direct_watch

    if force_live or not direct_watch:
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


def resolve_playback_youtube_url_phase2(row: pd.Series) -> str:
    link_url, _ = resolve_playback_youtube_targets_phase2(row=row, force_live=False, live_timeout=8)
    return str(link_url or "")


def _is_video_playlist_playable_phase2(video_id: str, timeout: int = 4) -> bool:
    key = str(video_id or "").strip()
    if not key:
        return False
    if key in _PLAYLIST_PLAYABLE_CACHE:
        return _PLAYLIST_PLAYABLE_CACHE[key]

    watch_url = f"https://www.youtube.com/watch?v={key}"

    # Probe 1: oEmbed must be reachable.
    oembed_url = "https://www.youtube.com/oembed?" + urlencode({"url": watch_url, "format": "json"})
    oembed_request = Request(
        oembed_url,
        method="GET",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        with urlopen(oembed_request, timeout=timeout) as response:
            oembed_ok = int(getattr(response, "status", 200) or 200) == 200
    except HTTPError as exc:
        if exc.code in {401, 403, 404, 410}:
            _PLAYLIST_PLAYABLE_CACHE[key] = False
            return False
        _PLAYLIST_PLAYABLE_CACHE[key] = False
        return False
    except URLError:
        _PLAYLIST_PLAYABLE_CACHE[key] = False
        return False
    except Exception:
        _PLAYLIST_PLAYABLE_CACHE[key] = False
        return False

    if not oembed_ok:
        _PLAYLIST_PLAYABLE_CACHE[key] = False
        return False

    # Probe 2: watch page playability status.
    watch_request = Request(
        watch_url,
        method="GET",
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urlopen(watch_request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="ignore")
        player_patterns = [
            r"var ytInitialPlayerResponse = (\{.*?\});",
            r"ytInitialPlayerResponse\s*=\s*(\{.*?\});",
        ]
        playability_status = ""
        for pattern in player_patterns:
            match = re.search(pattern, html, flags=re.DOTALL)
            if not match:
                continue
            try:
                payload = json.loads(match.group(1))
            except Exception:
                continue
            if isinstance(payload, dict):
                playability = payload.get("playabilityStatus", {})
                if isinstance(playability, dict):
                    playability_status = str(playability.get("status", "")).upper()
                    break
        if playability_status in {"UNPLAYABLE", "ERROR", "LOGIN_REQUIRED"}:
            _PLAYLIST_PLAYABLE_CACHE[key] = False
            return False
        _PLAYLIST_PLAYABLE_CACHE[key] = True
        return True
    except Exception:
        # If watch-page parse fails but oEmbed passed, keep item.
        _PLAYLIST_PLAYABLE_CACHE[key] = True
        return True


def build_playable_youtube_ids_phase2(
    queue: pd.DataFrame,
    max_ids: int = 50,
    max_live_resolves: int = 12,
    live_timeout: int = 4,
    min_ids_required: int = 2,
    fill_to_max: bool = False,
    max_total_seconds: float = 12.0,
    return_stats: bool = False,
) -> list[str] | tuple[list[str], dict[str, Any]]:
    cache: dict[tuple[object, ...], object] = base_app.st.session_state.setdefault(
        "_playable_youtube_ids_cache_phase2",
        {},
    )
    signature = tuple(
        (
            str(row.get("canonical_key", "")),
            str(row.get("youtube_link", "")),
            str(row.get("url_youtube", "")),
            str(row.get("youtube_video_id", "")),
        )
        for _, row in queue.head(max_ids).iterrows()
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
            cached_ids = list(cached_payload)  # type: ignore[arg-type]
            cached_stats = {}
        if return_stats:
            default_stats = {
                "playable_count": len(cached_ids),
                "playable_linked_rows": len(cached_ids),
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
        return ([], empty_stats) if return_stats else []

    resolve_budget = max_live_resolves if fill_to_max else min(max_live_resolves, target_rows)
    start_ts = time.time()
    youtube_ids: list[str] = []
    seen_ids: set[str] = set()
    row_diagnostics: list[dict[str, str]] = []

    included_direct = 0
    included_resolved = 0
    resolver_attempted = 0
    resolver_resolved = 0
    duplicate_rows = 0
    unresolved_rows = 0
    budget_blocked_rows = 0
    live_resolves_used = 0

    def _time_budget_exceeded() -> bool:
        if max_total_seconds <= 0:
            return False
        return (time.time() - start_ts) >= max_total_seconds

    for pos in range(target_rows):
        row = queue.iloc[pos]
        artist = str(row.get("artist", "") or "")
        track = str(row.get("track", "") or "")
        credits = str(row.get("artist_credits", "") or "")
        track_artist = f"{artist} - {track}".strip(" -")

        direct_watch = base_app.normalize_youtube_watch_url(row.get("youtube_url")) or base_app.normalize_youtube_watch_url(
            row.get("youtube_link")
        ) or base_app.normalize_youtube_watch_url(row.get("url_youtube"))

        chosen_watch = str(direct_watch or "").strip()
        source = "direct" if chosen_watch else "none"
        skip_reason = "-"
        skip_detail = "-"

        if not chosen_watch:
            if _time_budget_exceeded() or live_resolves_used >= resolve_budget:
                budget_blocked_rows += 1
                if not chosen_watch:
                    skip_reason = "resolver_budget_exhausted"
                    skip_detail = "live resolve budget/time limit reached"
            else:
                resolver_attempted += 1
                live_resolves_used += 1
                live_watch, live_reason, _ = base_app.resolve_live_youtube_watch_url(
                    artist,
                    track,
                    credits,
                    timeout=live_timeout,
                )
                live_watch = str(live_watch or "").strip()
                if live_watch:
                    resolver_resolved += 1
                    chosen_watch = live_watch
                    source = "resolved"
                elif not chosen_watch:
                    skip_reason = "resolver_no_match"
                    skip_detail = str(live_reason or "resolver returned empty watch URL")

        if not chosen_watch:
            fallback = str(row.get("youtube_url") or row.get("youtube_link") or row.get("url_youtube") or "").strip()
            if fallback:
                chosen_watch = fallback
                if source == "none":
                    source = "fallback"

        chosen_id = base_app.extract_youtube_video_id(chosen_watch)
        playlist_status = "skipped"
        playlist_source = source if source != "none" else "-"
        youtube_id_out = "-"

        if chosen_id:
            direct_trusted = source == "direct"
            if direct_trusted or _is_video_playlist_playable_phase2(chosen_id, timeout=max(3, live_timeout)):
                youtube_id_out = chosen_id
                if chosen_id in seen_ids:
                    duplicate_rows += 1
                    playlist_status = "duplicate"
                    skip_reason = "duplicate_youtube_id"
                    skip_detail = "duplicate collapsed in temporary playlist"
                else:
                    seen_ids.add(chosen_id)
                    youtube_ids.append(chosen_id)
                    playlist_status = "included"
                if source == "resolved":
                    included_resolved += 1
                else:
                    included_direct += 1
            else:
                unresolved_rows += 1
                skip_reason = "unplayable_on_youtube_playlist"
                skip_detail = "YouTube reports this ID unavailable for playlist playback"
        else:
            unresolved_rows += 1
            if skip_reason == "-":
                skip_reason = "no_youtube_id"
                skip_detail = "no direct video id after resolution"

        row_diagnostics.append(
            {
                "rank": str(pos + 1),
                "track_artist": track_artist or "-",
                "playlist_status": playlist_status,
                "playlist_source": playlist_source,
                "skip_reason": skip_reason,
                "skip_detail": skip_detail,
                "youtube_id": youtube_id_out,
            }
        )

    playable_count = len(youtube_ids)
    playable_linked_rows = included_direct + included_resolved + duplicate_rows
    stats = {
        "playable_count": playable_count,
        "playable_linked_rows": playable_linked_rows,
        "target_rows": target_rows,
        "included_direct": included_direct,
        "included_resolved": included_resolved,
        "resolver_attempted": resolver_attempted,
        "resolver_resolved": resolver_resolved,
        "duplicate_rows": duplicate_rows,
        "unresolved_rows": unresolved_rows,
        "budget_blocked_rows": budget_blocked_rows,
        "resolve_budget": resolve_budget,
        "row_diagnostics": row_diagnostics,
    }

    # Keep behavior compatible with existing UI messaging.
    if playable_count < min_ids_required and not fill_to_max:
        pass

    cache[cache_key] = {"ids": list(youtube_ids), "stats": stats, "cached_at": time.time()}
    return (youtube_ids, stats) if return_stats else youtube_ids


def patch_base_app_for_phase2() -> None:
    app_v2.patch_base_app_for_v2()
    base_app.DEFAULT_DATASET_ID = "managed://latest"
    base_app.read_source = read_source_phase2
    base_app.prepare_music_data_cached = prepare_music_data_cached_phase2
    base_app.resolve_live_youtube_watch_url = resolve_live_youtube_watch_url_phase2
    base_app.resolve_playback_youtube_targets = resolve_playback_youtube_targets_phase2
    base_app.resolve_playback_youtube_url = resolve_playback_youtube_url_phase2
    base_app.build_playable_youtube_ids = build_playable_youtube_ids_phase2


if __name__ == "__main__":
    patch_base_app_for_phase2()
    base_app.main()
