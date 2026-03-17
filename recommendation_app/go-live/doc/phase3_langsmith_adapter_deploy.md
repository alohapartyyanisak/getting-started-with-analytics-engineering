# Phase 3 Setup: LangSmith Adapter Deploy

## Purpose
This guide explains how to deploy the scheduler webhook adapter to Cloud Run using:

- `.github/workflows/go_live_langsmith_adapter_deploy.yml`

The adapter receives scheduler and retention webhook payloads, then forwards them into LangSmith as traces.

## Service Shape
- Runtime: Cloud Run
- Entrypoint: `recommendation_app/go-live/code/prod/langsmith_webhook_adapter.py`
- Public endpoint:
  - `GET /health`
  - `POST /langsmith-ingest`
- Protection: shared bearer token in `Authorization: Bearer ...`

This keeps the production scheduler flow simple:

`GitHub Actions scheduler -> adapter webhook -> LangSmith Trace API`

## Required Repository Secrets
- `GCP_PROJECT_ID`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`
- `GCP_SERVICE_ACCOUNT`
- `LANGSMITH_API_KEY`
- `LANGSMITH_ADAPTER_BEARER_TOKEN`

Optional:
- `LANGSMITH_WORKSPACE_ID`
- `GCP_SA_KEY` if not using OIDC

## Required Repository Variables
- `GCP_REGION`
- `GAR_REPOSITORY`
- `LANGSMITH_ADAPTER_SERVICE`

Recommended:
- `LANGSMITH_PROJECT=go-live-ops`
- `LANGSMITH_API_URL=https://api.smith.langchain.com`

## Local Env Workflow
- keep the broader workspace values in root `.env` if that is already your habit
- for go-live work, copy the adapter-related values into `recommendation_app/go-live/.env`
- use `recommendation_app/go-live/.env.example` as the starting template

Bootstrap the local working copy with:

```bash
python recommendation_app/go-live/code/prod/bootstrap_go_live_env.py
```

## Secret Source Modes
The deploy workflow supports two modes.

### 1. `github_secrets`
This is the fastest setup path.

The workflow reads:
- `LANGSMITH_API_KEY`
- `LANGSMITH_ADAPTER_BEARER_TOKEN`
- optional `LANGSMITH_WORKSPACE_ID`

Then it injects them into the Cloud Run service as runtime environment variables.

Use this mode when:
- you want the quickest first deploy
- you are still setting up the platform
- you do not want to create Secret Manager entries yet

### 2. `secret_manager`
This is the stricter production path.

The workflow binds these existing Google Secret Manager secrets to Cloud Run:
- `LANGSMITH_API_KEY`
- `LANGSMITH_ADAPTER_BEARER_TOKEN`

Optional:
- `LANGSMITH_WORKSPACE_ID` can still be passed as a normal env var if you use it

Use this mode when:
- you want Cloud Run runtime secrets backed by Google Secret Manager
- you want less secret material injected directly from GitHub at deploy time

## First Deploy Recommendation
Use `github_secrets` first.

It removes one moving part while we verify:
- OIDC auth
- Artifact Registry push
- Cloud Run deploy
- adapter health endpoint
- scheduler webhook delivery
- LangSmith trace ingestion

After that works, move to `secret_manager` if you want stricter secret handling.

## Workflow Inputs
Manual dispatch supports:
- `image_tag`
- `deploy_environment`
- `secret_source`

Recommended first run:
- `deploy_environment=staging`
- `secret_source=github_secrets`

## What The Workflow Does
1. Checks out the repo.
2. Auths to Google Cloud.
3. Builds the shared app image.
4. Pushes it to Artifact Registry.
5. Deploys a dedicated Cloud Run service for the adapter.
6. Sets the container command to:

```bash
python recommendation_app/go-live/code/prod/langsmith_webhook_adapter.py
```

7. Prints the deployed service URL.

## Post-Deploy Validation
After deploy:
1. Open `https://<adapter-url>/health`
2. Confirm it returns `ok`
3. Point:
   - `PROD_SCHED_NOTIFY_URL`
   - `PROD_RETENTION_NOTIFY_URL`
   to:
   - `https://<adapter-url>/langsmith-ingest`
4. Set:
   - `PROD_SCHED_NOTIFY_BEARER_TOKEN`
   - `PROD_RETENTION_NOTIFY_BEARER_TOKEN`
   to the same shared token used by the adapter
5. Manually dispatch the weekly scheduler workflow
6. Confirm:
   - scheduler run still succeeds
   - notification reports success
   - LangSmith receives the run trace

## Operational Notes
- The adapter should stay non-blocking from the scheduler perspective.
- If LangSmith ingestion fails, the scheduler run should still complete and record the notification failure separately.
- This service is for trace ingestion only. It should not become the main place where scheduler business logic lives.
