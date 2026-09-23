# syntax=docker/dockerfile:1.7
# Wapsi GNN scoring service (editions/amd/gnn): FastAPI on 8081, POST /score, GET /healthz.
# Build from the repository root (BuildKit reads editions/amd/gnn/Dockerfile.dockerignore):
#   ROCm (AMD Instinct MI300X):
#     DOCKER_BUILDKIT=1 docker build -f editions/amd/gnn/Dockerfile -t wapsi-amd-gnn:rocm .
#     docker run --rm --device /dev/kfd --device /dev/dri --group-add video --group-add render \
#       -v $PWD/data/generated/demo:/data/world:ro -v $PWD/editions/amd/gnn/checkpoints/<name>:/artifacts:ro \
#       -p 127.0.0.1:8081:8081 wapsi-amd-gnn:rocm
#   CPU fallback (any laptop):
#     docker build -f editions/amd/gnn/Dockerfile --build-arg BASE_IMAGE=python:3.12-slim --build-arg TORCH_CPU=1 \
#       -t wapsi-amd-gnn:cpu .
# BASE_IMAGE: the ROCm PyTorch image, pinned. The tag below is a placeholder chosen on 2026-09-23; re-pin it (tag and
# @sha256 digest) at kickoff to the image the AMD Developer Cloud MI300X nodes run (rocm/pytorch:<pinned tag>).
# The ROCm image already contains torch built for HIP; it is never reinstalled here (the torch.cuda API maps to HIP).

ARG BASE_IMAGE=rocm/pytorch:rocm7.1_ubuntu24.04_py3.12_pytorch_release_2.9.1
FROM ${BASE_IMAGE}

ARG TORCH_CPU=0
ARG TORCH_CPU_VERSION=2.9.1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONPATH=/app/src:/app \
    WAPSI_GNN_CHECKPOINT=/artifacts \
    WAPSI_GNN_WORLD=/data/world \
    WAPSI_GNN_DEVICE=auto \
    PORT=8081

WORKDIR /app

# runtime dependencies only (no training extras); torch comes from the base image, or the CPU wheel index
RUN if [ "$TORCH_CPU" = "1" ]; then \
        python3 -m pip install --index-url https://download.pytorch.org/whl/cpu "torch==${TORCH_CPU_VERSION}"; \
    fi \
 && python3 -m pip install "pydantic>=2.7" "numpy>=1.26" "pandas>=2.2" "pyarrow>=15" "fastapi>=0.115" \
        "uvicorn>=0.30" "safetensors>=0.6" "httpx>=0.27" \
 && python3 -c "import torch; print('torch', torch.__version__, 'hip', torch.version.hip)"

COPY src ./src
COPY editions/amd/gnn ./editions/amd/gnn

# non-root; on ROCm hosts pass --group-add video --group-add render (compose: group_add) for /dev/kfd and /dev/dri
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin wapsi \
 && mkdir -p /artifacts /data/world && chown -R wapsi /app
USER 10001

EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=4).status == 200 else 1)"
CMD ["python3", "-m", "editions.amd.gnn.service"]
