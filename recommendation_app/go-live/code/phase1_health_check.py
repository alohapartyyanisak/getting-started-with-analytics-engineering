from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def run_health_check() -> int:
    started = time.perf_counter()
    code_dir = Path(__file__).resolve().parents[2] / "offline-v2" / "code"
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))

    status = "unhealthy"
    details: dict[str, object] = {}

    try:
        import data_upgrade_v2  # type: ignore
        import recommender_v2_adapter  # type: ignore
    except Exception as exc:
        details["error"] = f"import failure: {exc}"
        print(json.dumps({"status": status, **details}, ensure_ascii=False, indent=2))
        return 2

    try:
        raw_df, source_path, dataset_path = data_upgrade_v2.load_and_merge_from_kagglehub()
        prepared_df = recommender_v2_adapter.prepare_music_data(raw_df)

        required_prepared_cols = [
            "artist",
            "track",
            "display_name",
            "duration_ms",
            "spotify_link",
            "youtube_link",
            "momentum_score",
            "popularity_norm",
        ]
        missing_cols = [col for col in required_prepared_cols if col not in prepared_df.columns]
        if missing_cols:
            raise ValueError(f"missing prepared columns: {missing_cols}")
        if prepared_df.empty:
            raise ValueError("prepared dataset is empty")

        status = "healthy"
        details = {
            "source_path": source_path,
            "dataset_path": dataset_path,
            "rows_raw": int(len(raw_df)),
            "rows_prepared": int(len(prepared_df)),
            "artists": int(prepared_df["artist"].nunique()),
            "tracks": int(prepared_df["display_name"].nunique()),
            "missing_prepared_columns": [],
        }
    except Exception as exc:
        details = {"error": str(exc)}

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = {"status": status, "elapsed_ms": elapsed_ms, **details}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(run_health_check())

