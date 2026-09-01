# data-observability-agent -- portable image so the reconciliation + agent
# pipeline runs in any orchestrator (cron, Airflow, Argo, plain `docker
# run`), not just GitHub Actions. See docs/docker.md.
#
# What is NOT in the image, by design: .env, real credentials, and any
# seeded/warehouse .duckdb files. All of that is supplied at runtime via
# environment variables and a mounted volume (see .dockerignore).

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DUCKDB_PATH=/data/warehouse.duckdb \
    RESULTS_STORE_PATH=/data/results.duckdb

RUN pip install uv==0.11.28

WORKDIR /app

# Dependencies first so the layer caches independently of app-code changes.
# --no-install-project: this is an app run from source (PYTHONPATH=/app),
# not a packaged library, so uv only needs to install the locked deps.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code. config/ is copied so the image is runnable stand-alone,
# but docs/docker.md shows mounting your own config/environments.yml over it.
COPY dbt_project/ ./dbt_project/
COPY mock_erp/ ./mock_erp/
COPY connectors/ ./connectors/
COPY reconciliation/ ./reconciliation/
COPY results_store/ ./results_store/
COPY agent/ ./agent/
COPY model_docs/ ./model_docs/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY examples/ ./examples/

RUN mkdir -p /data
VOLUME ["/data"]

# `docker run <image> data-load`  /  `docker run <image> code-change --sql-diff-file ...`
ENTRYPOINT ["python", "scripts/run_check.py"]
CMD ["--help"]
