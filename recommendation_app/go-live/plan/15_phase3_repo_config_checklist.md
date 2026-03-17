# Phase 3 Repo Config Checklist

Use this checklist before running the first Cloud Run app deploy and LangSmith adapter deploy.

## Local Working Copy
- keep root `.env` if you use it for the wider workspace
- create `recommendation_app/go-live/.env` as the local working copy for go-live
- start from `recommendation_app/go-live/.env.example`
- keep `recommendation_app/go-live/code/.env.sample` only as the smaller runtime subset
- bootstrap with:
  - `python recommendation_app/go-live/code/prod/bootstrap_go_live_env.py`

## GitHub Secrets

### Core Google Cloud
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`

Optional fallback:
- `GCP_SA_KEY`

### Dataset Storage
- `GCS_DATASET_URI`

### Scheduler / Retention Notification
- `PROD_SCHED_NOTIFY_URL`
- `PROD_SCHED_NOTIFY_BEARER_TOKEN`
- `PROD_RETENTION_NOTIFY_URL`
- `PROD_RETENTION_NOTIFY_BEARER_TOKEN`

### LangSmith Adapter
- `LANGSMITH_API_KEY`
- `LANGSMITH_ADAPTER_BEARER_TOKEN`

Optional:
- `LANGSMITH_WORKSPACE_ID`

### Existing Resolver Secrets
- `YOUTUBE_API_KEY`
- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`

## GitHub Variables

### App Hosting
- `GCP_REGION`
- `GAR_REPOSITORY`
- `CLOUD_RUN_SERVICE`

### LangSmith Adapter Hosting
- `LANGSMITH_ADAPTER_SERVICE`
- `LANGSMITH_PROJECT`
- `LANGSMITH_API_URL`

Recommended defaults:
- `LANGSMITH_PROJECT=go-live-ops`
- `LANGSMITH_API_URL=https://api.smith.langchain.com`

## Google Cloud Services To Enable
- Cloud Run API
- Artifact Registry API
- IAM Credentials API
- Cloud Build API

If using `secret_manager` mode for the adapter:
- Secret Manager API

## Google Cloud Resources To Create

### Required
- GCS bucket for dataset storage
- Artifact Registry repository for container images
- Cloud Run service for app
- Cloud Run service for LangSmith adapter

### Optional but recommended
- Secret Manager entries for:
  - `LANGSMITH_API_KEY`
  - `LANGSMITH_ADAPTER_BEARER_TOKEN`

Optional:
- Secret Manager entry for `LANGSMITH_WORKSPACE_ID`

## IAM Expectations

### GitHub deploy identity should be able to
- push images to Artifact Registry
- deploy Cloud Run revisions
- read secret references if workflow uses Secret Manager bindings

### Runtime app service account should be able to
- read from `GCS_DATASET_URI`

### Scheduler identity should already be able to
- read/write dataset bucket
- maintain snapshots, pointer, and logs

## Workflow-to-config Mapping

### `.github/workflows/go_live_cloud_run_deploy.yml`
Needs:
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GCS_DATASET_URI`
- `GCP_REGION`
- `GAR_REPOSITORY`
- `CLOUD_RUN_SERVICE`

### `.github/workflows/go_live_langsmith_adapter_deploy.yml`
Needs:
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `GCP_REGION`
- `GAR_REPOSITORY`
- `LANGSMITH_ADAPTER_SERVICE`
- `LANGSMITH_API_KEY`
- `LANGSMITH_ADAPTER_BEARER_TOKEN`
- optional `LANGSMITH_WORKSPACE_ID`
- `LANGSMITH_PROJECT`
- `LANGSMITH_API_URL`

Also choose a deploy mode:
- `github_secrets`
- `secret_manager`

### `.github/workflows/go_live_weekly_scheduler.yml`
Already uses:
- `GCS_DATASET_URI`
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `PROD_SCHED_NOTIFY_URL`
- `PROD_SCHED_NOTIFY_BEARER_TOKEN`
- resolver secrets

## Recommended First Execution Order
1. Deploy app service to Cloud Run.
2. Open app URL and verify it loads promoted `latest.json`.
3. Deploy LangSmith adapter to Cloud Run using `github_secrets` mode first.
4. Point scheduler and retention webhook secrets to adapter URL.
5. Dispatch weekly scheduler manually.
6. Confirm:
   - scheduler run succeeds
   - webhook delivery succeeds
   - LangSmith receives traces

Optional hardening after first success:
7. Move adapter runtime secrets to Secret Manager mode.

## Ready / Not Ready Decision
Ready when:
- all required secrets and vars exist
- app deploy workflow has a valid target service
- adapter deploy workflow has valid LangSmith secrets
- promoted snapshot in GCS is loadable

Not ready when:
- deploy workflows are missing required vars
- Cloud Run target services are undefined
- OIDC auth is not valid
- GCS pointer is not on the intended promoted snapshot
