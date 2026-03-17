#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-alohapartyyanisak/getting-started-with-analytics-engineering}"
WEEKLY_WORKFLOW=".github/workflows/go_live_weekly_scheduler.yml"
RETENTION_WORKFLOW=".github/workflows/go_live_monthly_retention.yml"

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

set_secret_if_present() {
  local name="$1"
  local value="${!name:-}"
  if [[ -n "$value" ]]; then
    printf "%s" "$value" | gh secret set "$name" --repo "$REPO"
    echo "set secret: $name"
  else
    echo "skipped secret (missing env): $name"
  fi
}

require_cmd gh
gh auth status >/dev/null

echo "repo: $REPO"

echo "== Setting required secrets (if present) =="
set_secret_if_present "GCP_PROJECT_ID"
set_secret_if_present "GCS_DATASET_URI"

echo "== Setting auth secrets (provide one auth method) =="
set_secret_if_present "GCP_WORKLOAD_IDENTITY_PROVIDER"
set_secret_if_present "GCP_SERVICE_ACCOUNT"
set_secret_if_present "GCP_SA_KEY"

echo "== Setting optional notification/api secrets =="
set_secret_if_present "PROD_SCHED_NOTIFY_URL"
set_secret_if_present "PROD_SCHED_NOTIFY_BEARER_TOKEN"
set_secret_if_present "PROD_RETENTION_NOTIFY_URL"
set_secret_if_present "PROD_RETENTION_NOTIFY_BEARER_TOKEN"
set_secret_if_present "YOUTUBE_API_KEY"
set_secret_if_present "SPOTIFY_CLIENT_ID"
set_secret_if_present "SPOTIFY_CLIENT_SECRET"

echo "== Dispatch weekly workflow =="
gh workflow run "$WEEKLY_WORKFLOW" \
  --repo "$REPO" \
  -f mode="problem_queue_bounded" \
  -f problem_max_rows="1200" \
  -f problem_max_resolve_keys="400" \
  -f post_smoke="true" \
  -f notify_on="error"

echo "== Dispatch monthly retention workflow (apply=false) =="
gh workflow run "$RETENTION_WORKFLOW" \
  --repo "$REPO" \
  -f apply="false" \
  -f retention_days="60" \
  -f dry_run_snapshot_retention_days="14" \
  -f run_log_retention_days="30" \
  -f notify_on="error"

echo "== Latest runs =="
gh run list --repo "$REPO" --workflow "$WEEKLY_WORKFLOW" -L 1
gh run list --repo "$REPO" --workflow "$RETENTION_WORKFLOW" -L 1

echo "Done. After verification, keep schedules enabled in both workflow files."
