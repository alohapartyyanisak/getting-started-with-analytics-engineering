from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
from phase_shared_dataset import load_prepared_dataset_for_phase_checks


def _build_queue_for_playlist(app_module, playlist: pd.DataFrame) -> pd.DataFrame:
    queue = playlist.copy().reset_index(drop=True)
    queue["position"] = queue.index + 1
    queue["duration_text"] = queue["duration_ms"].apply(app_module.format_track_duration)
    queue["spotify_url"] = queue.apply(
        lambda row: row.get("spotify_link") or row.get("url_spotify") or "",
        axis=1,
    )
    queue["youtube_url"] = queue.apply(
        lambda row: app_module.prefer_direct_youtube_url(row.get("youtube_link"), row.get("url_youtube")),
        axis=1,
    )
    return queue


def _linked_ids_in_rank_order(row_diagnostics: list[dict[str, str]]) -> list[str]:
    ordered: list[str] = []
    for item in row_diagnostics:
        if str(item.get("playlist_status", "")) != "linked":
            continue
        youtube_id = str(item.get("youtube_id", "")).strip()
        if youtube_id and youtube_id != "-":
            ordered.append(youtube_id)
    return ordered


def _check_real_data_case(app_module, recommender_module, data: pd.DataFrame) -> dict[str, object]:
    top_songs = data.sort_values(["momentum_score", "views", "stream"], ascending=False)["display_name"].head(5).tolist()
    if len(top_songs) < 2:
        return {
            "name": "real_data",
            "status": "fail",
            "error": "Insufficient top songs to build regression playlist.",
        }

    rec = recommender_module.recommend_tracks(
        data=data,
        seed_display_name={top_songs[0]: 0.5, top_songs[1]: 0.5},
        mood="Balanced",
        spotify_weight=0.6,
        discovery_mode=0.25,
        top_k=180,
        exclude_seed_tracks=True,
        preferred_artist_weights={},
    )
    playlist = recommender_module.build_duration_playlist(
        recommendations=rec,
        target_minutes=120,
        tolerance_minutes=3,
        candidate_limit=180,
        max_tracks=50,
    )
    if playlist.empty:
        return {
            "name": "real_data",
            "status": "fail",
            "error": "Playlist is empty for real data case.",
        }

    queue = _build_queue_for_playlist(app_module, playlist)
    youtube_ids, stats = app_module.build_playable_youtube_ids(
        queue,
        max_ids=50,
        max_live_resolves=0,
        live_timeout=3,
        min_ids_required=2,
        fill_to_max=False,
        return_stats=True,
    )
    row_diagnostics = stats.get("row_diagnostics", [])
    if not isinstance(row_diagnostics, list):
        return {
            "name": "real_data",
            "status": "fail",
            "error": "row_diagnostics missing or invalid.",
        }

    linked_ids = _linked_ids_in_rank_order(row_diagnostics)
    duplicate_count = len(youtube_ids) - len(set(youtube_ids))
    order_match = youtube_ids == linked_ids[: len(youtube_ids)]
    passed = bool(duplicate_count == 0 and order_match)

    return {
        "name": "real_data",
        "status": "pass" if passed else "fail",
        "playlist_rows": int(len(playlist)),
        "linked_rows": int(sum(1 for row in row_diagnostics if str(row.get("playlist_status", "")) == "linked")),
        "youtube_ids_count": int(len(youtube_ids)),
        "duplicate_ids": int(duplicate_count),
        "order_match": bool(order_match),
    }


def _check_synthetic_backfill_case(app_module) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for i in range(1, 51):
        youtube = f"https://www.youtube.com/watch?v=VID{i:03d}" if i != 28 else ""
        rows.append(
            {
                "artist": f"Artist {i}",
                "track": f"Track {i}",
                "artist_credits": f"Artist {i}",
                "youtube_url": youtube,
                "youtube_link": youtube,
                "url_youtube": youtube,
                "duration_ms": 180000,
                "spotify_link": f"https://open.spotify.com/track/track{i:03d}",
                "url_spotify": f"https://open.spotify.com/track/track{i:03d}",
            }
        )
    queue = pd.DataFrame(rows)

    original_resolve = app_module.resolve_live_youtube_watch_url
    original_playable = app_module._is_youtube_video_playlist_playable
    call_counts: dict[str, int] = {}

    def fake_resolve(artist: str, track: str, credits: str, timeout: int = 4) -> tuple[str, str, float]:
        key = f"{artist} - {track}"
        call_counts[key] = call_counts.get(key, 0) + 1
        if key == "Artist 28 - Track 28":
            if call_counts[key] == 1:
                return "", "", 0.0
            return "https://www.youtube.com/watch?v=VID928", "synthetic", 9.0
        return "", "", 0.0

    try:
        app_module.resolve_live_youtube_watch_url = fake_resolve
        app_module._is_youtube_video_playlist_playable = lambda video_id: True
        youtube_ids, stats = app_module.build_playable_youtube_ids(
            queue=queue,
            max_ids=50,
            max_live_resolves=50,
            live_timeout=1,
            min_ids_required=2,
            fill_to_max=False,
            return_stats=True,
        )
    finally:
        app_module.resolve_live_youtube_watch_url = original_resolve
        app_module._is_youtube_video_playlist_playable = original_playable

    row_diagnostics = stats.get("row_diagnostics", [])
    if not isinstance(row_diagnostics, list) or len(row_diagnostics) < 28:
        return {
            "name": "synthetic_backfill_order",
            "status": "fail",
            "error": "row_diagnostics missing synthetic rank rows.",
        }

    row28 = row_diagnostics[27]
    row28_status_ok = (
        str(row28.get("playlist_status", "")) == "linked"
        and str(row28.get("playlist_source", "")) == "resolved_backfill"
        and str(row28.get("youtube_id", "")) == "VID928"
    )
    rank_order_ok = len(youtube_ids) >= 28 and youtube_ids[27] == "VID928"
    duplicate_count = len(youtube_ids) - len(set(youtube_ids))
    passed = bool(row28_status_ok and rank_order_ok and duplicate_count == 0)

    return {
        "name": "synthetic_backfill_order",
        "status": "pass" if passed else "fail",
        "row28_status_ok": bool(row28_status_ok),
        "rank_order_ok": bool(rank_order_ok),
        "duplicate_ids": int(duplicate_count),
        "youtube_ids_count": int(len(youtube_ids)),
    }


def run_regression() -> int:
    code_dir = Path(__file__).resolve().parents[2] / "offline-v2" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    import app as offline_app  # type: ignore
    import recommender_v2_adapter  # type: ignore

    data, loader_meta = load_prepared_dataset_for_phase_checks(prefer_managed=True)
    if data.empty:
        payload = {"status": "fail", "error": "Prepared dataset is empty.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    results = [
        _check_real_data_case(offline_app, recommender_v2_adapter, data),
        _check_synthetic_backfill_case(offline_app),
    ]
    failed = [item["name"] for item in results if item.get("status") != "pass"]
    payload = {
        "status": "pass" if not failed else "fail",
        "failed_cases": failed,
        "cases": results,
        "loader_meta": loader_meta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run_regression())
