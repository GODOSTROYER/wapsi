# syntax=docker/dockerfile:1.7
# Wapsi, Google Cloud edition: one image, two Cloud Run services.
#   API (default CMD):  ADK API + Pub/Sub trigger + /healthz    -> uvicorn editions.gcp.app.server:app
#   UI  (--command):    shared Streamlit app on the case store   -> streamlit run editions/gcp/ui_app.py
# Build from the repository root (BuildKit reads editions/gcp/Dockerfile.dockerignore):
#   DOCKER_BUILDKIT=1 docker build -f editions/gcp/Dockerfile -t wapsi-gcp .
# Local demo run (no cloud access):
#   docker run --rm -p 8080:8080 -e WAPSI_DEMO_MODE=1 -e WAPSI_ADK_WEB=1 wapsi-gcp

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

WORKDIR /app

# 1. dependencies only (cached layer); versions come from uv.lock
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra gcp --extra ui --extra docs

# 2. application code and the public legal corpus (synthetic data only; no generated worlds)
COPY src ./src
COPY ui ./ui
COPY .streamlit ./.streamlit
COPY data/docs ./data/docs
COPY data/samples ./data/samples
COPY evals ./evals
COPY editions/gcp ./editions/gcp
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra gcp --extra ui --extra docs

# 3. optional: the synthetic judging world (~2 MB: demo seed 7 + the two showcase cases; the same world that
#    deploy.ps1 / deploy.sh load into BigQuery), so WAPSI_LEDGER=parquet WAPSI_WORLD=/app/data/world runs without
#    BigQuery and WAPSI_SEED_CASES=showcase finds its cases. BAKE_WORLD=0 skips it.
ARG BAKE_WORLD=1
RUN if [ "${BAKE_WORLD}" = "1" ]; then \
      /app/.venv/bin/python -m editions.gcp.app.seed world --out /app/data/world; \
    fi

# 4. unprivileged runtime user; the app never writes to its own tree
RUN groupadd --system --gid 10001 wapsi \
 && useradd --system --uid 10001 --gid wapsi --home-dir /home/wapsi --create-home wapsi \
 && chown -R root:root /app && chmod -R a+rX /app
USER 10001:10001

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/wapsi \
    PORT=8080 \
    WAPSI_DEMO_MODE=0 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8080

# Cloud Run ignores HEALTHCHECK; probes are set in deploy/cloudrun.sh (/healthz for the API,
# /_stcore/health for the UI).
CMD ["sh", "-c", "exec uvicorn editions.gcp.app.server:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 8"]
