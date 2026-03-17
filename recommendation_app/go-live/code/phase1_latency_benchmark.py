from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from phase_shared_dataset import load_prepared_dataset_for_phase_checks


def _percentile_ms(values_ms: list[float], q: float) -> float:
    if not values_ms:
        return 0.0
    return float(np.percentile(np.asarray(values_ms, dtype=float), q))


def run_latency(iterations: int, p95_threshold_ms: int) -> int:
    code_dir = Path(__file__).resolve().parents[2] / "offline-v2" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    import recommender_v2_adapter  # type: ignore

    data, loader_meta = load_prepared_dataset_for_phase_checks(prefer_managed=True)
    if data.empty:
        payload = {"status": "fail", "error": "Prepared dataset is empty.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    top_songs = data.sort_values(["momentum_score", "views", "stream"], ascending=False)["display_name"].head(80).tolist()
    if not top_songs:
        payload = {"status": "fail", "error": "No top songs available for latency benchmark.", "loader_meta": loader_meta}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    rng = np.random.default_rng(seed=20260302)
    durations_ms: list[float] = []

    for _ in range(max(1, int(iterations))):
        seed_count = min(len(top_songs), int(rng.integers(1, 4)))
        if seed_count <= 0:
            break
        seed_choices = rng.choice(top_songs, size=seed_count, replace=False).tolist()
        weights = np.asarray([float(rng.random()) + 0.1 for _ in seed_choices], dtype=float)
        weights = weights / weights.sum()
        seed_weights = {str(name): float(weight) for name, weight in zip(seed_choices, weights)}

        mood = str(rng.choice(["Balanced", "High Energy", "Chill", "Dark", "Uplifting"]))
        spotify_weight = float(rng.uniform(0.35, 0.75))
        discovery_mode = float(rng.uniform(0.0, 1.0))
        target_minutes = int(rng.integers(45, 181))

        start = time.perf_counter()
        rec = recommender_v2_adapter.recommend_tracks(
            data=data,
            seed_display_name=seed_weights,
            mood=mood,
            spotify_weight=spotify_weight,
            discovery_mode=discovery_mode,
            top_k=180,
            exclude_seed_tracks=(discovery_mode < 0.99),
            preferred_artist_weights={},
        )
        _ = recommender_v2_adapter.build_duration_playlist(
            recommendations=rec,
            target_minutes=target_minutes,
            tolerance_minutes=3,
            candidate_limit=180,
            max_tracks=50,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        durations_ms.append(elapsed_ms)

    p50 = _percentile_ms(durations_ms, 50)
    p95 = _percentile_ms(durations_ms, 95)
    payload = {
        "iterations": len(durations_ms),
        "latency_ms": {
            "min": round(min(durations_ms), 2) if durations_ms else 0.0,
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "max": round(max(durations_ms), 2) if durations_ms else 0.0,
            "mean": round(statistics.mean(durations_ms), 2) if durations_ms else 0.0,
        },
        "threshold_ms": int(p95_threshold_ms),
        "status": "pass" if p95 <= float(p95_threshold_ms) else "fail",
        "loader_meta": loader_meta,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure recommendation+playlist latency.")
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--p95-threshold-ms", type=int, default=2500)
    args = parser.parse_args()
    raise SystemExit(run_latency(args.iterations, args.p95_threshold_ms))
