# Phase 3 Setup: Cloud Run App Deploy

## Purpose
This guide explains how to deploy the `go-live` Streamlit app to Cloud Run using the repository workflow:

- `.github/workflows/go_live_cloud_run_deploy.yml`

This deploy path assumes the scheduler and GCS dataset contract are already working, and that the app should read the promoted snapshot from object storage at runtime.

## Deployment Shape
- App runtime: Cloud Run
- Image registry: Artifact Registry
- Dataset source of truth: `GCS_DATASET_URI`
- CI/CD runner: GitHub Actions
- Auth: GitHub OIDC to Google Cloud

The app image does not need to bake a specific approved dataset into the container.

Instead, at startup it runs:

- `recommendation_app/go-live/code/prod/sync_storage_from_gcs.py`

That syncs the storage tree from `GCS_DATASET_URI` into the container runtime path and then starts the app.

For local setup consistency:
- use `recommendation_app/go-live/.env.example` as the canonical go-live env template
- use `recommendation_app/go-live/code/.env.sample` as the smaller app-runtime subset
- keep your root `.env` as the broader workspace source if helpful, but copy the go-live-needed values into `recommendation_app/go-live/.env`

Bootstrap the local working copy with:

```bash
python recommendation_app/go-live/code/prod/bootstrap_go_live_env.py
```

## Required Repository Secrets
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GCS_DATASET_URI`

Fallback only if not using OIDC:
- `GCP_SA_KEY`

## Required Repository Variables
- `GCP_REGION`
- `GAR_REPOSITORY`
- `CLOUD_RUN_SERVICE`

Recommended values:
- `GCP_REGION=us-central1` or your chosen primary region
- `GAR_REPOSITORY=go-live`
- `CLOUD_RUN_SERVICE=dj-mixing-station-go-live`

## What The Workflow Does
1. Checks out the repo.
2. Auths to Google Cloud.
3. Builds the root Docker image.
4. Pushes the image to Artifact Registry.
5. Deploys the image to Cloud Run.
6. Passes:
   - `GCS_DATASET_URI`
   - `GO_LIVE_SYNC_FROM_GCS=true`
   - `GO_LIVE_DATASET_STORAGE_ROOT=/tmp/go-live/data/storage`
7. Prints the deployed service URL.

## Manual Deploy
Run via GitHub Actions:

- workflow: `go-live-cloud-run-deploy`
- ref: `go-live`

Optional inputs:
- `image_tag`
- `deploy_environment`

If `image_tag` is blank, the workflow uses `GITHUB_SHA`.

## Runtime Contract
Cloud Run app runtime should rely on:
- promoted `latest.json`
- snapshot metadata under `snapshots/<dataset_version>/`

It should not depend on:
- Kaggle credentials
- ad hoc local CSV uploads for production
- runtime resolver inventing new answers for already-fixed rows

## Health Expectations
The current container health check uses:

```bash
python recommendation_app/go-live/code/phase2_health_check.py
```

This validates:
- managed dataset path is loadable
- the prepared dataset contract is still valid

For Phase 3, this is acceptable as a startup/readiness signal even though the app itself is Streamlit.

## First Deploy Checklist
Before first deploy:
1. Confirm Artifact Registry repository exists.
2. Confirm Cloud Run API is enabled.
3. Confirm the deploy service account can:
   - push images
   - deploy Cloud Run
   - read GCS dataset bucket if needed at runtime
4. Confirm `GCS_DATASET_URI/latest.json` points to the approved snapshot.
5. Confirm the promoted snapshot is loadable locally.

## Post-Deploy Smoke
After deploy:
1. Open the Cloud Run service URL.
2. Confirm the app starts without local-only assumptions.
3. Confirm the loaded dataset version matches promoted `latest.json`.
4. Run one smoke playlist flow.
5. Confirm no crash on first load.

## Notes
- This workflow is for app hosting only.
- Scheduler publishing remains on:
  - `.github/workflows/go_live_weekly_scheduler.yml`
- Retention remains on:
  - `.github/workflows/go_live_monthly_retention.yml`
- LangSmith adapter deployment is separate and uses:
  - `.github/workflows/go_live_langsmith_adapter_deploy.yml`
