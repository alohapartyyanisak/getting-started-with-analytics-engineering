# Phase 3 Plan: App Hosting, Scheduler Sync, and LangSmith Tracing

## Objective
Ship the promoted `go-live` app on managed hosting, keep dataset delivery on the existing scheduler + GCS contract, and add trace-level observability through the existing webhook adapter pattern.

## Current Foundation Already Completed
- Phase 2 data productization is operational.
- Versioned dataset snapshots are published and promoted through `latest.json`.
- GitHub Actions scheduler is working with:
  - OIDC / Workload Identity
  - GCS storage sync
  - bounded problem-queue runs
  - explicit promote / rollback flow
- Portable snapshot metadata paths are in place.
- LangSmith webhook adapter design already exists in:
  - `recommendation_app/go-live/doc/prod_scheduler_langsmith_adapter.md`

This means Phase 3 should focus on app delivery and observability integration, not rebuilding the scheduler.

## Hosting Decision
- Preferred host: Google Cloud Run
- Scheduler: GitHub Actions
- Dataset storage: Google Cloud Storage
- Trace / run observability: LangSmith via webhook adapter

This is the cleanest fit for the current repo because:
- scheduler auth is already on Google Cloud
- storage is already on GCS
- Cloud Run supports simple HTTPS deployment and revision rollback
- LangSmith can be added without making scheduler success depend on observability delivery

## Steps

### 1. Containerize app
- Add reproducible `Dockerfile`.
- Add canonical go-live env template (`.env.example`) and minimal runtime subset (`.env.sample`).
- Ensure container runs Streamlit on `$PORT`.
- Keep the app capable of forcing a reviewed snapshot via `GO_LIVE_DATASET_VERSION`.

Exit criteria:
- Image builds locally.
- Container starts with the promoted snapshot contract.
- Container health check passes.

### 2. Provision managed app hosting
- Create Cloud Run service for the app.
- Configure runtime env vars and service account.
- Point hosted runtime to the Phase 2 dataset contract in GCS.
- Keep `latest.json` as the only active dataset pointer.

Exit criteria:
- Hosted app starts successfully in staging.
- Hosted runtime can load the approved snapshot.

### 3. CI/CD for app delivery
- Add GitHub Actions workflow to:
  - build image
  - tag image
  - push image
  - deploy to Cloud Run
- Support `workflow_dispatch` for controlled releases.
- Keep deployed revision traceable to git commit and image tag.

Exit criteria:
- Manual deploy works end to end.
- Revision/image mapping is visible and repeatable.

### 4. Scheduler-to-host alignment
- Reuse the existing weekly scheduler path.
- Confirm scheduler output and hosting runtime use the same storage contract.
- Keep auto-promote disabled by default.
- Continue manual validation before promotion.

Exit criteria:
- Scheduler writes candidate snapshots to GCS.
- App runtime reads the promoted snapshot only.

### 5. LangSmith trace integration
- Deploy the LangSmith webhook adapter as a small HTTP service.
- Point:
  - `PROD_SCHED_NOTIFY_URL`
  - `PROD_RETENTION_NOTIFY_URL`
  to the adapter endpoint.
- Reuse shared bearer-token validation.
- Send scheduler and retention run payloads into LangSmith traces.
- Include stage-level timing, dataset versions, and promotion outcome.

Exit criteria:
- Scheduler and retention runs appear in LangSmith.
- Trace delivery remains non-blocking for pipeline success.

### 6. Domain and TLS
- Provision domain for the hosted app.
- Attach HTTPS / TLS.
- Verify routing and certificate renewal path.

Exit criteria:
- Public staging URL is reachable over TLS.

### 7. Health, logs, and smoke validation
- Add container/runtime health checks.
- Add structured logs with dataset version stamp.
- Add staging smoke checks for:
  - app startup
  - snapshot load
  - single-song embed behavior
  - temporary playlist behavior
  - debug-view consistency

Exit criteria:
- Hosted app emits actionable health and log signals.
- Smoke checks pass against the promoted snapshot.

### 8. Rollback controls and runbook
- Verify app release rollback using Cloud Run revision history.
- Verify dataset rollback using pointer restore.
- Document combined rollback runbook.
- Run one rollback drill in staging.

Exit criteria:
- Both app and dataset rollback paths are tested.

### 9. Public release gate
- Run staging release.
- Validate app, scheduler, and LangSmith traces together.
- Promote public deployment only after acceptance.

Exit criteria:
- Phase 3 exit criteria in `01_master_plan.md` are satisfied.

## Verification
1. Build local image:
   - `docker build -t dj-mixing-station-go-live .`
2. Run local container:
   - `docker run --rm -p 8505:8080 --env-file recommendation_app/go-live/.env.example dj-mixing-station-go-live`
3. App runtime check:
   - UI loads on local container
   - container health check passes
4. Deploy workflow check:
   - `workflow_dispatch` builds and deploys image
   - deployed revision matches expected image tag
5. Scheduler integration check:
   - weekly/manual scheduler writes candidate snapshot to GCS
   - promoted pointer remains the app source of truth
6. Trace check:
   - scheduler webhook reaches LangSmith adapter
   - LangSmith receives root run + stage runs
7. Rollback drill:
   - previous app revision restored
   - previous dataset version restored

## Decisions
- Host on Cloud Run, not Streamlit Community Cloud, for production parity and rollback control.
- Keep GitHub Actions as the scheduler runner.
- Keep GCS as the dataset contract boundary.
- Use LangSmith through the webhook adapter, not by coupling scheduler logic directly to LangSmith calls.
- Keep manual promotion gate in place even after hosting rollout.

## Scope Boundary
- App hosting and scheduler trace integration belong in Phase 3.
- Full dashboards, alert routing, and broader observability hardening remain Phase 4 work.

## Next Implementation Order
1. `Dockerfile` + `go-live/.env.example` + `.env.sample`
2. Cloud Run deploy workflow
3. LangSmith webhook adapter deployment wiring
4. Staging deploy and smoke validation
