from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
from phase_shared_dataset import load_prepared_dataset_for_phase_checks


def _nonempty_duplicate_count(values: pd.Series) -> int:
    series = values.fillna("").astype(str).str.strip()
    nonempty = series[series != ""]
    return int(nonempty.duplicated(keep=False).sum())


_HTTP_URL_RE = re.compile(r"^https?://", flags=re.IGNORECASE)


def _malformed_link_count(values: pd.Series) -> int:
    series = values.fillna("").astype(str).str.strip()
    nonempty = series[series != ""]
    if nonempty.empty:
        return 0
    return int((~nonempty.str.match(_HTTP_URL_RE)).sum())


def _rows_without_any_link(playlist: pd.DataFrame) -> int:
    spotify_values = playlist.get("spotify_link", pd.Series(index=playlist.index, dtype=str)).fillna("").astype(str).str.strip()
    youtube_values = playlist.get("youtube_link", pd.Series(index=playlist.index, dtype=str)).fillna("").astype(str).str.strip()
    missing_mask = (spotify_values == "") & (youtube_values == "")
    return int(missing_mask.sum())


def run_regression() -> int:
    code_dir = Path(__file__).resolve().parents[2] / "offline-v2" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    import recommender_v2_adapter  # type: ignore

    data, loader_meta = load_prepared_dataset_for_phase_checks(prefer_managed=True)
    if data.empty:
        payload = {"status": "fail", "error": "Prepared dataset is empty.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    top_songs = data.sort_values(["momentum_score", "views", "stream"], ascending=False)["display_name"].head(6).tolist()
    if len(top_songs) < 2:
        payload = {
            "status": "fail",
            "error": "Insufficient top songs to run regression benchmark.",
            "top_songs_count": len(top_songs),
            "loader_meta": loader_meta,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    top_artists = (
        data.groupby("artist", as_index=False)
        .agg(stream=("stream", "sum"), views=("views", "sum"), momentum=("momentum_score", "mean"))
        .assign(score=lambda d: d["stream"] + d["views"] + d["momentum"] * 1_000_000)
        .sort_values("score", ascending=False)["artist"]
        .head(3)
        .tolist()
    )
    if not top_artists:
        payload = {"status": "fail", "error": "No artists available for regression benchmark.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    artist_seed_map = (
        data.sort_values(["momentum_score", "views", "stream"], ascending=False)
        .drop_duplicates(subset=["artist"], keep="first")
        .set_index("artist")["display_name"]
        .to_dict()
    )
    if top_artists[0] not in artist_seed_map:
        payload = {
            "status": "fail",
            "error": "Top artist seed mapping missing.",
            "top_artist": top_artists[0],
            "loader_meta": loader_meta,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    cases = [
        {
            "name": "hits_single_seed",
            "seed_weights": {top_songs[0]: 1.0},
            "mood": "High Energy",
            "spotify_weight": 0.65,
            "discovery_mode": 0.0,
            "exclude_seed_tracks": False,
            "preferred_artist_weights": {},
            "target_minutes": 120,
        },
        {
            "name": "balanced_multi_seed",
            "seed_weights": {top_songs[0]: 0.5, top_songs[1]: 0.5},
            "mood": "Balanced",
            "spotify_weight": 0.55,
            "discovery_mode": 0.35,
            "exclude_seed_tracks": True,
            "preferred_artist_weights": {},
            "target_minutes": 90,
        },
        {
            "name": "hidden_gems_artist_bias",
            "seed_weights": {artist_seed_map[top_artists[0]]: 1.0},
            "mood": "Chill",
            "spotify_weight": 0.50,
            "discovery_mode": 1.0,
            "exclude_seed_tracks": True,
            "preferred_artist_weights": {top_artists[0]: 1.0},
            "target_minutes": 75,
        },
    ]

    case_results: list[dict[str, object]] = []
    failures: list[str] = []

    for case in cases:
        rec = recommender_v2_adapter.recommend_tracks(
            data=data,
            seed_display_name=case["seed_weights"],
            mood=str(case["mood"]),
            spotify_weight=float(case["spotify_weight"]),
            discovery_mode=float(case["discovery_mode"]),
            top_k=180,
            exclude_seed_tracks=bool(case["exclude_seed_tracks"]),
            preferred_artist_weights=case["preferred_artist_weights"],
        )
        playlist = recommender_v2_adapter.build_duration_playlist(
            recommendations=rec,
            target_minutes=int(case["target_minutes"]),
            tolerance_minutes=3,
            candidate_limit=180,
            max_tracks=50,
        )

        sorted_ok = True
        if not rec.empty:
            score_diff = rec["recommendation_score"].diff().fillna(0.0)
            sorted_ok = bool((score_diff <= 1e-12).all())

        spotify_dup = _nonempty_duplicate_count(playlist.get("spotify_link", pd.Series(dtype=str)))
        youtube_dup = _nonempty_duplicate_count(playlist.get("youtube_link", pd.Series(dtype=str)))
        spotify_malformed = _malformed_link_count(playlist.get("spotify_link", pd.Series(dtype=str)))
        youtube_malformed = _malformed_link_count(playlist.get("youtube_link", pd.Series(dtype=str)))
        rows_without_links = _rows_without_any_link(playlist)

        passed = bool(
            (not rec.empty)
            and (not playlist.empty)
            and sorted_ok
            and spotify_dup == 0
            and youtube_dup == 0
            and spotify_malformed == 0
            and youtube_malformed == 0
            and rows_without_links == 0
        )
        if not passed:
            failures.append(str(case["name"]))

        case_results.append(
            {
                "case": case["name"],
                "recommendation_rows": int(len(rec)),
                "playlist_rows": int(len(playlist)),
                "recommendation_sorted_desc": sorted_ok,
                "playlist_spotify_duplicate_links": spotify_dup,
                "playlist_youtube_duplicate_links": youtube_dup,
                "playlist_spotify_malformed_links": spotify_malformed,
                "playlist_youtube_malformed_links": youtube_malformed,
                "playlist_rows_without_any_link": rows_without_links,
                "passed": passed,
            }
        )

    payload = {
        "status": "pass" if not failures else "fail",
        "failed_cases": failures,
        "cases": case_results,
        "loader_meta": loader_meta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(run_regression())
