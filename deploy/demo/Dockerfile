# syntax=docker/dockerfile:1.7
# Wapsi public demo: the shared officer app (Streamlit), edition-neutral and AMD-first. One container, one port.
#   - cases:  the two showcase cases + the synthetic demo world (seed 7), baked at build time
#   - model:  an OpenAI-compatible endpoint when WAPSI_LLM_BASE_URL / WAPSI_LLM_API_KEY / WAPSI_LLM_MODEL are set at
#             run time (AMD Token Factory, or your own vLLM on AMD); otherwise a labelled recorded run
#             ("Recorded run - live AMD endpoint not configured"). No key is ever baked into the image.
# Build from the repository root (BuildKit reads deploy/demo/Dockerfile.dockerignore):
#   DOCKER_BUILDKIT=1 docker build -f deploy/demo/Dockerfile -t wapsi-demo .
# Run (recorded mode, no key):       docker run --rm -p 7860:7860 wapsi-demo
# Run (live, key from your shell):   docker run --rm -p 7860:7860 -e WAPSI_LLM_BASE_URL -e WAPSI_LLM_API_KEY \
#                                      -e WAPSI_LLM_MODEL wapsi-demo
# Hugging Face Spaces (Docker SDK) builds this same file from the folder scripts/make_space.py assembles; Spaces
# run the container as user 1000 and route to port 7860. Hosting guide: deploy/DEMO_HOSTING.md.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

# the runtime user (Hugging Face Spaces run the container as uid 1000); the app tree stays owned by root
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin wapsi

WORKDIR /app

# 1. dependencies only (cached layer); versions come from uv.lock. Core + UI + the PDF reader for the docs index;
#    no torch, no cloud SDKs.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra ui --extra docs

# 2. application code, the public legal corpus, the synthetic voice samples, the recorded prose and the KPI report
COPY src ./src
COPY ui ./ui
COPY .streamlit ./.streamlit
COPY data/docs ./data/docs
COPY data/samples ./data/samples
COPY data/recordings ./data/recordings
COPY evals/reports/latest.json ./evals/reports/latest.json
COPY deploy/demo ./deploy/demo
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra ui --extra docs

# 3. the synthetic demo world (seed 7, 39 cases, about 2 MB), so the sidebar's Data switch offers it at once.
#    BAKE_WORLD=0 skips it; the app then generates it at its first start if the tree is writable, else it shows
#    the showcase cases only.
ARG BAKE_WORLD=1
RUN if [ "${BAKE_WORLD}" = "1" ]; then /app/.venv/bin/python deploy/demo/prepare.py; fi

# 4. the docs search index (BM25 over the public legal corpus), so clause search and the citation quotes work on
#    the deployed URL. data/docs/index is not in the repository (the documents carry no open licence, so their text
#    is never committed): when the build context has no index it is built here from the public sources (polite,
#    TLS-verified downloads; the raw files are not kept). If that fails the build still succeeds and the app serves
#    the citations-only corpus. The documents' extracted full texts (index/text) are not needed at run time and are
#    removed. BAKE_DOCS_INDEX=0 skips it.
ARG BAKE_DOCS_INDEX=1
RUN if [ "${BAKE_DOCS_INDEX}" = "1" ]; then \
      /app/.venv/bin/python -m wapsi.search.bake --fetch --prune-raw && rm -rf /app/data/docs/index/text; \
    fi

# 5. readable by the unprivileged user; the app never writes to its own tree (caches go to /tmp)
RUN chmod -R a+rX /app/src /app/ui /app/.streamlit /app/data /app/evals /app/deploy
USER 1000:1000

ENV PATH="/app/.venv/bin:${PATH}" \
    HOME=/home/wapsi \
    PORT=7860 \
    WAPSI_UI_LLM=auto \
    WAPSI_LLM_CACHE_DIR=/tmp/wapsi-llm-cache \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT', '7860'), timeout=4)" || exit 1

# XSRF protection and CORS checks are off because Spaces serve the app inside an iframe on another origin, where
# Streamlit's uploads (the Voice Intake page) are otherwise refused with HTTP 403. The demo holds synthetic data
# only and has no login.
CMD ["sh", "-c", "exec streamlit run deploy/demo/app.py --server.address 0.0.0.0 --server.port ${PORT} --server.headless true --server.enableXsrfProtection false --server.enableCORS false --browser.gatherUsageStats false"]
