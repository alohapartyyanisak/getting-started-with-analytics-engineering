from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from phase_shared_dataset import load_prepared_dataset_for_phase_checks


def _count_nonempty_dups(series: pd.Series) -> int:
    values = series.fillna("").astype(str).str.strip()
    values = values[values != ""]
    return int(values.duplicated(keep=False).sum())


def run_duplicate_validation(scenarios: int = 20) -> int:
    code_dir = Path(__file__).resolve().parents[2] / "offline-v2" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    import recommender_v2_adapter  # type: ignore

    data, loader_meta = load_prepared_dataset_for_phase_checks(prefer_managed=True)
    if data.empty:
        payload = {"status": "fail", "error": "Prepared dataset is empty.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    top_songs = data.sort_values(["momentum_score", "views", "stream"], ascending=False)["display_name"].head(120).tolist()
    if not top_songs:
        payload = {"status": "fail", "error": "No top songs available for duplicate validation.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    rng = np.random.default_rng(seed=777)
    failed = 0
    records: list[dict[str, object]] = []

    for i in range(max(1, int(scenarios))):
        seed_count = min(len(top_songs), int(rng.integers(1, 4)))
        if seed_count <= 0:
            break
        seed_names = rng.choice(top_songs, size=seed_count, replace=False).tolist()
        seed_weights = {name: 1.0 / seed_count for name in seed_names}
        discovery_mode = float(rng.uniform(0.0, 1.0))

        rec = recommender_v2_adapter.recommend_tracks(
            data=data,
            seed_display_name=seed_weights,
            mood=str(rng.choice(["Balanced", "High Energy", "Chill", "Dark", "Uplifting"])),
            spotify_weight=float(rng.uniform(0.3, 0.8)),
            discovery_mode=discovery_mode,
            top_k=180,
            exclude_seed_tracks=(discovery_mode < 0.99),
            preferred_artist_weights={},
        )
        playlist = recommender_v2_adapter.build_duration_playlist(
            recommendations=rec,
            target_minutes=int(rng.integers(45, 181)),
            tolerance_minutes=3,
            candidate_limit=180,
            max_tracks=50,
        )

        spotify_dup = _count_nonempty_dups(playlist.get("spotify_link", pd.Series(dtype=str)))
        youtube_dup = _count_nonempty_dups(playlist.get("youtube_link", pd.Series(dtype=str)))
        ok = bool(spotify_dup == 0 and youtube_dup == 0)
        if not ok:
            failed += 1
        records.append(
            {
                "scenario": i + 1,
                "playlist_rows": int(len(playlist)),
                "spotify_duplicate_links": spotify_dup,
                "youtube_duplicate_links": youtube_dup,
                "pass": ok,
            }
        )

    payload = {
        "status": "pass" if failed == 0 else "fail",
        "scenarios": len(records),
        "failed": failed,
        "results": records,
        "loader_meta": loader_meta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run_duplicate_validation())
