# Sunday Soft Launch Runbook

This runbook defines the first 6 hours of the controlled public go-live for:

- `https://studio.djmixingstation.com`

The goal is to launch carefully, watch the public lane closely, and keep rollback simple if something drifts.

## Launch Mode

- launch type: controlled soft public launch
- operator mode: single operator on watch
- launch window: first half of Sunday
- monitoring window: first 4-6 hours

## Preconditions

These must already be green before launch starts:

- `studio` smoke
- `dev` smoke
- scheduler artifact smoke
- weekly scheduler dry run
- rollback drill

## Guardrails

- do not deploy during the launch window unless there is a real incident
- do not overlap manual rollback, promote, or scheduler control-plane actions
- keep Cloud Armor in preview mode
- treat background scanner traffic as operational noise, not product demand

## Launch Start

Recommended launch time:

- Sunday at `11:00 AM EDT`

At launch start:

1. confirm `studio.djmixingstation.com` loads normally
2. confirm Dashboard A is live and updating
3. confirm alerts are armed:
   - p95 latency
   - error rate
   - playlist pipeline failures
   - resolver degradation
4. confirm latest public revision is expected

## First 30 Minutes

Watch:

- Cloud Monitoring dashboard
- Cloud Logging operational events
- Cloud Run request logs

Check:

- `request_received`
- `response_sent`
- `resolver_quality_evaluated`
- no sustained `error` burst
- no visible user-facing break in manual spot check

Expected state:

- some traffic may be scanner/bot traffic
- public lane remains responsive
- no alert should fire continuously

## Hour 1

Manual checks:

1. open `studio`
2. generate one playlist
3. verify playback controls and temporary playlist link
4. verify structured request logs still land

Product checks if minimum product event layer is deployed:

- `session_started`
- `landing_viewed`
- `mode_selected`
- `controls_changed`
- `playlist_generated`

## Hours 2-4

Review every 30-60 minutes:

- request volume
- response volume
- p95 latency
- error rate
- playlist pipeline failures
- resolver degradation count

Product review if enabled:

- sessions
- playlist activation
- time to first playlist

## Hours 4-6

Decision:

- continue normal soft launch
- remain in caution mode
- rollback if needed

Continue if:

- no sustained alert
- no repeated manual product failure
- public lane remains responsive

Use caution if:

- latency is elevated but stable
- isolated errors are present but not growing
- scanner traffic is noisy but app health is intact

Rollback if:

- sustained 5xx behavior
- repeated playlist generation failure
- resolver degradation clusters with user-visible impact
- manual user journey is broken

## Rollback Path

Use the current rollback runbook:

- `recommendation_app/go-live/plan/10_rollback_runbook.md`

Preferred rollback order:

1. release rollback if the app revision is the problem
2. dataset rollback if the promoted snapshot is the problem

## Launch-Day Notes

- product metrics and operational metrics must be read separately
- Cloud Monitoring answers reliability questions
- product-event logs answer adoption/activation questions
- scanner traffic may inflate request logs but should not be treated as user success
