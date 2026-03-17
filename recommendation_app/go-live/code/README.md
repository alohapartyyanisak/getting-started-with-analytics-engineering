# go-live code

This folder is reserved for production-grade service code (API, orchestration, deployment scripts).

## Phase 1 Validation Scripts

- `phase1_health_check.py`
  - Startup health + schema fail-fast validation.
- `phase1_regression_benchmark.py`
  - Deterministic regression benchmark for recommendation outputs.
- `phase1_latency_benchmark.py`
  - p50/p95 benchmark for recommendation + playlist latency.
- `phase1_duplicate_link_validation.py`
  - Automated duplicate Spotify/YouTube link suppression validation.

Run from repo root:

```bash
python3 recommendation_app/go-live/code/phase1_health_check.py
python3 recommendation_app/go-live/code/phase1_regression_benchmark.py
python3 recommendation_app/go-live/code/phase1_latency_benchmark.py --iterations 25 --p95-threshold-ms 2500
python3 recommendation_app/go-live/code/phase1_duplicate_link_validation.py
```

## Phase 0 / Phase 2 Data Artifacts Generator

- `generate_phase0_data_artifacts.py`
  - Generates:
    - overlap/collision audit report (`T-003`)
    - source registry metadata table (`T-005`)
    - snapshot manifest with hashes + row counts (`T-004`)

```bash
python3 recommendation_app/go-live/code/generate_phase0_data_artifacts.py
```

## Phase 2 Data Productization Scripts

- `phase2_publish_dataset.py`
  - Builds and publishes a versioned prepared dataset snapshot to managed storage root.
  - Updates pointer file (`latest.json` by default).
- `phase2_incremental_publish.py`
  - Real delta upsert publisher:
    - detects source changes via source fingerprint
    - classifies rows as `insert` / `update` / `ignore` by `canonical_key`
    - upserts only changed rows into current snapshot
    - publishes a new version only when content changes
    - supports optional cloud artifact upload via URI root (`s3://`, `gs://`, `abfs://`, etc. via `fsspec` backends)
- `phase2_managed_loader.py`
  - Managed dataset resolver/loader + prepared schema validation.
- `phase2_health_check.py`
  - Health check for managed dataset runtime path.
- `phase2_rollback_smoke.py`
  - Verifies at least one-step dataset version rollback is loadable.
- `application_phase2.py`
  - Streamlit entrypoint that loads managed snapshots (Phase 2 path).
  - Includes explicit temporary YouTube playlist diagnostics in Debug View:
    - `Playlist Link`, `Link Source`, `Skip Reason`, `Skip Detail`, `YouTube ID`
  - Coverage caption distinguishes:
    - `rows linked` vs `unique YouTube IDs`

Run from repo root:

```bash
python3 recommendation_app/go-live/code/phase2_publish_dataset.py
python3 recommendation_app/go-live/code/phase2_incremental_publish.py
python3 recommendation_app/go-live/code/phase2_health_check.py
python3 recommendation_app/go-live/code/phase2_rollback_smoke.py
streamlit run recommendation_app/go-live/code/application_phase2.py --server.port 8505 --server.fileWatcherType none
```

Env vars (optional):
- `GO_LIVE_DATASET_STORAGE_ROOT`
- `GO_LIVE_DATASET_POINTER_FILE` (default: `latest.json`)
- `GO_LIVE_DATASET_VERSION` (force specific version)
- `GO_LIVE_DATASET_URI` (explicit root path / file URI)
- `GO_LIVE_PRIMARY_ARTIFACT_ROOT_URI` (optional cloud artifact root URI, e.g. `s3://my-bucket/dj-mixing-station`)

## Phase 3 Container Runtime

Files added for Phase 3 hosting bootstrap:

- root `Dockerfile`
- root `.dockerignore`
- `recommendation_app/go-live/.env.example`
- `recommendation_app/go-live/code/.env.sample`

Build from repo root:

```bash
docker build -t dj-mixing-station-go-live .
```

Run locally:

```bash
docker run --rm -p 8505:8080 --env-file recommendation_app/go-live/.env.example dj-mixing-station-go-live
```

Container behavior:
- runs Streamlit on `0.0.0.0:$PORT`
- defaults `PORT=8080`
- uses `GO_LIVE_DATASET_STORAGE_ROOT=/app/recommendation_app/go-live/data/storage`
- can sync dataset storage from `GCS_DATASET_URI` at startup when `GO_LIVE_SYNC_FROM_GCS=true`
- keeps support for `GO_LIVE_DATASET_VERSION` when you want to force a reviewed snapshot
- uses `recommendation_app/go-live/.env.example` as the canonical go-live env template

Container health signal:

```bash
python recommendation_app/go-live/code/phase2_health_check.py
```
