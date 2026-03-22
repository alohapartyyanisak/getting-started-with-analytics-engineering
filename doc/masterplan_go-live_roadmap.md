# DJ Mixing Station Studio - Master Go-Live Plan

## Objective
Deliver a stable, secure, and scalable live version so external users can use the app without local setup or Kaggle credential steps.

## Current State (Offline)
- UI and ranking logic are in Streamlit + Python.
- Dataset is loaded via KaggleHub or manual CSV upload.
- Runtime is local machine oriented.

## Target State (Live)
- Public URL with managed auth and uptime.
- Preprocessed dataset hosted in managed storage.
- App deployment with CI/CD, monitoring, alerts, and rollback.
- No end-user dependency on Kaggle keys.

## Current Delivery Status (as of 2026-03-02)
- Phase 1 stabilization is complete (`T-006`, `T-007`, `T-008`, `T-009`, `T-022`, `T-023`).
- Validation evidence is documented in:
  - `recommendation_app/go-live/plan/11_phase1_validation_report.md`
- Data planning artifacts completed:
  - `T-003` overlap/collision audit report
  - `T-005` source registry metadata table
  - `T-004` snapshot manifest with hashes/row counts
- Next focus:
  - Complete remaining core merge tasks `T-001` and `T-002`, then advance Phase 3 hosting tasks (`T-010`, `T-011`, `T-012`, `T-013`).

## Proposed Enhancements (Now / Next / Later)
This section captures suggested improvements for prioritization. It does not replace committed phase scope.

| Area | Suggestion | Priority |
|---|---|---|
| Architecture | Keep Streamlit for MVP UI; add thin API layer behind it when usage grows | `Now` |
| Architecture | Add FastAPI endpoints (`/recommend`, `/explain`, `/playlist`, `/health`) | `Next` |
| Architecture | Isolate ranking logic as shared package/module for API, UI, tests, batch eval | `Now` |
| Data | Treat KaggleHub as dev-only source | `Now` |
| Data | Publish versioned datasets to object storage and load from managed URI | `Now` |
| Data | Add schema contract validation (Pydantic or equivalent) to prevent silent breakage | `Now` |
| Data | Support dual enrichment execution modes: offline/manual for small updates, cloud/scheduled for larger weekly queues | `Now` |
| Data | Add Postgres metadata store for track IDs, embeddings, and precomputed stats | `Next` |
| Spotify/YouTube | Keep current embed flow for MVP; add Spotify OAuth only if account playback is needed | `Next` |
| Spotify/YouTube | Move temporary YouTube playlist creation to async backend job | `Next` |
| Spotify/YouTube | Cache playlist creation by `(anchor_set, controls, dataset_version)` | `Now` |
| Instrumentation | Log core events (`session_start`, `mode_selected`, `anchor_added/removed`, `controls_changed`, `recommendations_shown`, `track_clicked`, `spotify_opened`, `youtube_opened`, `playlist_created`) | `Now` |
| Metrics | Track activation, engagement, quality proxies, and control effectiveness | `Now` |
| Reliability | Add health checks, latency/error monitoring, dependency failure metrics | `Now` |
| Reliability | Add ranking drift checks (score distribution, concentration, novelty) | `Next` |
| Reliability | Add automated weekly snapshot validation gates so manual review becomes exception-based over time | `Next` |
| Safety/Privacy | Minimize personal data storage; define retention/deletion if account data is added | `Now` |
| Safety/Privacy | Add input sanitization and endpoint rate limiting | `Now` |
| UX Trust | Keep debug view understandable (top factors + confidence labels) | `Now` |
| Deployment | Phase 1 deploy: Streamlit container on managed host with caching | `Now` |
| Deployment | Phase 2 deploy: FastAPI + Postgres + background jobs | `Next` |
| Experimentation | Phase 3: feature flags + A/B testing for levers and ranking changes | `Later` |

## Migration Phases

### Phase 0: Offline Dataset Expansion (2-3 weeks)
- Onboard additional offline datasets.
- Harmonize schemas into a single contract.
- Apply canonical dedupe and row-quality scoring.
- Validate ranking regression and runtime performance.

Exit criteria:
- Expanded catalog is production-candidate quality in offline mode.
- Regression and performance checks pass defined thresholds.

### Phase 1: Stabilize for Production (1-2 weeks)
- Freeze scoring behavior and document expected outputs.
- Add deterministic smoke tests for core recommendation flows.
- Separate config from code (`.env`/secrets manager).
- Add dataset schema validation and startup health checks.

Exit criteria:
- Core regression tests pass.
- App starts with explicit pass/fail health signal.

### Phase 2: Data Productization (1 week)
- Build one enrichment pipeline with two execution modes:
  - offline/manual for small or ad hoc updates
  - cloud/scheduled for larger weekly queues or unattended runs
- Keep one shared publish contract regardless of execution mode:
  - finished versioned dataset snapshot in object storage
  - metadata bundle (`version`, `created_at`, `row_count`, schema hash, validation summary)
  - validation and revalidation reports
- Keep resolver, playability checks, and publish validation logic identical across offline and cloud runs.
- Treat runtime resolver as fallback only; the production app should primarily read resolved YouTube fields from the published snapshot.

Exit criteria:
- Live app reads from managed storage only.
- Rollback to previous dataset version is possible.
- Execution mode can change without changing the live app contract or snapshot format.

Execution mode decision rule:
- Choose offline/manual when the weekly update scope is small, the problem queue is limited, manual QA is expected, or the run can complete comfortably on a local machine.
- Choose cloud/scheduled when the weekly queue is larger, unattended execution is preferred, retries/checkpoints are needed, or the run duration is too long for reliable local execution.
- Regardless of execution mode, publish only after validation passes and the output snapshot format matches the production contract.

### Phase 3: App Hosting and Delivery (1-2 weeks)
- Containerize app.
- Deploy to managed runtime (Streamlit Community Cloud for beta or container platform for production).
- Add CDN/edge TLS and domain.
- Add per-release deployment pipeline.

Exit criteria:
- Public URL works for anonymous users.
- Blue/green or quick rollback path is verified.

### Phase 4: Security + Observability + Rollback Controls (1 week)
- Secrets in managed vault only.
- Basic abuse/rate protections.
- Implement structured log schema and version stamping on every request.
- Build dashboards and alert rules for latency, errors, resolver quality, and duplicate suppression.
- Add runtime rollback controls (dataset/configo/resolver/release) via flags.
- Persist resolver cache, unresolved/problem queue state, and run reports so offline and cloud executions can share the same recovery path.
- Add automated behavioral acceptance checks for single-song embed, temporary playlist generation, and debug-view consistency against the published snapshot contract.
- Prepare incident rollback runbook and execute rollback drills.
- Error budget and uptime target defined.

Exit criteria:
- Alerts fire on failures and are actionable.
- No hardcoded credentials/tokens in repo or UI.
- Rollback paths are validated end-to-end in staging.
- Logging schema is stable and consumed by monitoring.

### Phase 5: Beta and Scale Validation (1-2 weeks)
- Invite pilot users.
- Track latency, crash rate, and completion funnel.
- Tune caching and candidate limits.
- Fix top UX blockers.

Exit criteria:
- p95 response time under target.
- User completion rate meets baseline.

### Phase 6: Public Go-Live (1 week)
- Launch comms and changelog.
- Enable support rotation and incident runbook.
- Lock release branch and post-launch monitoring cadence.

Exit criteria:
- 7-day post-launch stable operations.

## Success Metrics
- Availability: >= 99.5% monthly.
- p95 recommendation generation latency: <= 2.5s (excluding first cold load).
- Playlist generation success rate: >= 99%.
- Crash-free sessions: >= 99.5%.
- First playlist completion rate: target defined during beta.
- Unresolved YouTube ratio: <= 20% (with resolver/cached fallback active).
- Duplicate link ratio in final playlist: <= 1%.

## Go/No-Go Checklist
- Security review approved.
- Data pipeline rollback tested.
- Release rollback tested.
- Resolver-off and cache-only fallback tested.
- Structured logging schema deployed (`09_logging_schema.md`) and validated.
- Rollback runbook drill passed (`10_rollback_runbook.md`).
- Monitoring and on-call tested.
- Top 10 user journeys validated.
