# syntax=docker/dockerfile:1.7
# Wapsi, AMD edition: pipeline + FastAPI (8080) + Streamlit UI (8501) + the ADK officer agent (the "agent" extra:
# google-adk + litellm, no Google Cloud client). CPU-only image; the GPU work happens in the vLLM-ROCm (llm,
# verifier-llm) and GNN containers, or behind any OpenAI-compatible endpoint (WAPSI_LLM_BASE_URL).
# Build from the repository root (BuildKit reads editions/amd/app/Dockerfile.dockerignore):
#   DOCKER_BUILDKIT=1 docker build -f editions/amd/app/Dockerfile -t wapsi-amd-app .
# Offline demo on any laptop (FakeLLM, showcase ledger, no GPU):
#   docker run --rm -p 127.0.0.1:8501:8501 -e WAPSI_DEMO_MODE=1 wapsi-amd-app
# Pin the base images by digest in editions/amd/.env (serving/pull_and_pin.sh fills PYTHON_IMAGE / UV_IMAGE).

ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS runtime

# fastapi / uvicorn are not part of a project extra yet; these match the versions in uv.lock
ARG FASTAPI_VERSION=0.141.1
ARG UVICORN_VERSION=0.53.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

# 1. dependencies only (cached layer); versions come from uv.lock
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra ui --extra docs --extra agent \
 && uv pip install --python /app/.venv/bin/python "fastapi==${FASTAPI_VERSION}" "uvicorn==${UVICORN_VERSION}"

# 2. application code and the public legal corpus (synthetic data only; worlds are mounted at run time)
COPY src ./src
COPY ui ./ui
COPY .streamlit ./.streamlit
COPY data/docs ./data/docs
COPY editions/shared ./editions/shared
COPY editions/amd/app ./editions/amd/app
COPY editions/amd/bench ./editions/amd/bench
COPY editions/amd/serving/smoke_test.py ./editions/amd/serving/smoke_test.py
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --inexact --no-dev --extra ui --extra docs --extra agent \
 && sed -i 's/\r$//' editions/amd/app/entrypoint.sh

# 3. unprivileged runtime user; the app writes only to /state (case store) and bench results
RUN groupadd --system --gid 10001 wapsi \
 && useradd --system --uid 10001 --gid wapsi --home-dir /home/wapsi --create-home wapsi \
 && install -d -o 10001 -g 10001 /state /app/editions/amd/bench/results \
 && chown -R root:root /app/src /app/ui /app/editions/amd/app /app/editions/shared && chmod -R a+rX /app
USER 10001:10001

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    HOME=/home/wapsi \
    WAPSI_DEMO_MODE=0 \
    WAPSI_STATE_DIR=/state \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 8080 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=4); u.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)" || exit 1

ENTRYPOINT ["bash", "/app/editions/amd/app/entrypoint.sh"]
