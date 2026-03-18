FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    GO_LIVE_DATASET_STORAGE_ROOT=/tmp/go-live/data/storage \
    GO_LIVE_SYNC_FROM_GCS=true

WORKDIR /app

COPY requirements.txt ./

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY recommendation_app ./recommendation_app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python recommendation_app/go-live/code/phase2_health_check.py >/tmp/phase2_health.json || exit 1

CMD ["python", "recommendation_app/go-live/code/prod/run_cloud_run_app.py"]
