from __future__ import annotations

import json
import time

import phase2_runtime_config as cfg
from phase2_managed_loader import load_prepared_dataset, release_summary


def run_health_check() -> int:
    started = time.perf_counter()
    status = "unhealthy"
    details: dict[str, object] = {}

    try:
        prepared_df, release = load_prepared_dataset(version=cfg.DATASET_VERSION or None)
        status = "healthy"
        details = {
            "dataset_root": str(cfg.dataset_root().resolve()),
            "dataset_version": release.version,
            "rows_prepared": int(len(prepared_df)),
            "artists": int(prepared_df["artist"].nunique()),
            "tracks": int(prepared_df["display_name"].nunique()),
            "release": release_summary(release),
        }
    except Exception as exc:
        details = {"error": str(exc)}

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = {"status": status, "elapsed_ms": elapsed_ms, **details}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status == "healthy" else 2


if __name__ == "__main__":
    raise SystemExit(run_health_check())

