# Phase 4 Dev Lane Smoke Hardening Checklist

## Goal
Keep `dev.djmixingstation.com` smooth enough for 1-2 developers without changing the visible UX/UI. Reuse the successful ideas from `studio` in the smoke harness and hidden/internal app markers only.

## Confirmation Bar
1. 3 consecutive `dev` smoke passes
2. 1 manual sanity pass on `https://dev.djmixingstation.com`

## 1. Separate Dev From Studio
### As-is
- `studio` is the production truth and release gate.
- `dev` is a richer debugging lane.

### To-be
- `dev` has its own smoke contract and never inherits `studio` UX assumptions.

### How
- Keep the `developer` profile separate in `hosted_interactive_smoke.mjs`.
- Keep `dev` smoke non-blocking.

## 2. Shell Readiness Before Interaction
### As-is
- `dev` often loads partially before player widgets hydrate.

### To-be
- Smoke proves shell readiness first.

### How
- Wait for hidden startup health.
- Wait for `Choose your move`.
- Wait for seed/mode controls.
- Do not use `Now Playing` as the first proof of life.

## 3. Result Surface Readiness
### As-is
- `Now Playing` can be too strict as the first post-selection readiness signal.

### To-be
- Smoke accepts any real result surface.

### How
- Accept any of:
  - hidden result-ready marker
  - hidden player-ready marker
  - hidden temp-playlist-ready marker
  - hidden debug-table-ready marker
  - `Now Playing`
  - `Playback`
  - `Open Temporary YouTube Playlist`
  - `Temporary playlist coverage:`

## 4. Hidden Internal Markers
### As-is
- Visible `dev` UX should remain unchanged.

### To-be
- Add invisible smoke markers only.

### How
- `data-testid="startup-health-status"`
- `data-testid="dev-final-picks-count"`
- `data-testid="dev-result-surface-ready"`
- `data-testid="dev-player-surface-ready"`
- `data-testid="dev-temp-playlist-ready"`
- `data-testid="dev-debug-table-ready"`

## 5. Embeds Are Optional For Smoke Readiness
### As-is
- YouTube embeds are blocked in smoke by design.

### To-be
- Embed network failures do not decide pass/fail.

### How
- Keep YouTube/embed blocking in the smoke harness.
- Verify player controls/surfaces, not remote embed success.

## 6. One Recovery Path Only
### As-is
- Partial hydration still happens under smoke.

### To-be
- Smoke gets one recovery chance without hiding real failures.

### How
- Warm app.
- Allow one reload/re-check path in the smoke harness.
- Keep one actual smoke attempt per workflow run.

## 7. Triage Rules
### As-is
- Different failures all look like “smoke failed”.

### To-be
- Categorize quickly.

### How
- `page_loaded = false`: runtime/infra issue
- shell ready but controls missing: hydration/widget issue
- picks locked in but results missing: result-surface issue
- playlist/player visible but assertion fails: smoke mismatch

## 8. Manual Sanity Pass
After 3 smoke passes:
1. Open `https://dev.djmixingstation.com`
2. Pick artists and/or songs
3. Confirm playlist appears
4. Confirm Spotify/YouTube controls appear
5. Confirm player updates on track change
6. Confirm debug view still renders
7. Confirm no obvious flicker/reset

## 9. If Smoke Still Fails Repeatedly
### Next internal steps
1. Keep visible `dev` UX unchanged
2. Add more hidden render-phase markers only if needed
3. Add request/render phase logging if smoke diagnosis stalls
4. Compare one manual-good session with one smoke-failed session

## Current Working Rule
- Use `studio` concepts in tests and hidden instrumentation.
- Do not simplify or redesign the visible `dev` lane for smoke convenience.
