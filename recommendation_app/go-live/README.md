# go-live

- `code/`: reserved for production service code
- `doc/`: go-live facing docs
- `plan/`: go-live planning artifacts

## Local Env Workflow
- Keep the root `.env` as your broader workspace source if you want.
- For `go-live` development, copy the needed values into `recommendation_app/go-live/.env`.
- Start from:
  - `recommendation_app/go-live/.env.example`
- Use:
  - `recommendation_app/go-live/code/.env.sample`
  only when you want the smaller app-runtime subset.

Bootstrap command:

```bash
python recommendation_app/go-live/code/prod/bootstrap_go_live_env.py
```

Recommended starting doc:
- `doc/phase1_local_to_live_readiness_guide.md`
- `doc/phase1_readiness_guide_general.md` (generic, reusable version)
- `doc/prod_scheduler_setup_main_article.md` (GitHub Actions + GCP scheduler setup overview)
