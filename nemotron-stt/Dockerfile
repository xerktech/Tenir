# nemotron-stt: NVIDIA Nemotron cache-aware streaming ASR (NeMo) behind a WebSocket
# /v1/audio/stream surface. It serves the LIVE-PARTIAL half of the hybrid STT path;
# the offline Parakeet server keeps the FINAL. See server.py for the why, and
# docs/stt-model-gpu-benchmark.md for the GPU benchmark that motivated it.
#
# Base: the NGC NeMo container (tested torch + CUDA + NeMo-ASR). Pinned via ARG so
# the tag is trivial to bump — and it MUST carry Blackwell (sm_120) kernels, because
# the GPU box is a Blackwell card. nemotron-3.5-asr-streaming is a new model class,
# so it needs a NeMo build recent enough to know it; bump NEMO_IMAGE if an older base
# can't load the weights. Multi-GB base → the release build runs on the self-hosted
# runner (like parakeet-stt / vllm-stt), never a hosted one.
ARG NEMO_IMAGE=nvcr.io/nvidia/nemo:25.09
FROM ${NEMO_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir Nemotron streaming STT (NeMo) — WebSocket /v1/audio/stream"

WORKDIR /app

# hadolint ignore=DL3013 — pip pins live in requirements.txt; the base pins torch/NeMo.
COPY nemotron-stt/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY nemotron-stt/server.py ./server.py

# The multilingual cache-aware streaming model (40 locales + auto language detection).
# HF_HOME points at the mounted cache so the weights persist across container
# recreation (compose mounts the whole /root/.cache to also cover NeMo's torch cache).
# NEMOTRON_ATT_CONTEXT_RIGHT=3 is the 320 ms operating point the GPU benchmark picked.
ENV NEMOTRON_MODEL=nvidia/nemotron-3.5-asr-streaming-0.6b \
    NEMOTRON_ATT_CONTEXT_RIGHT=3 \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8000

# One worker: a single resident model on one GPU; each WebSocket owns its own cache
# state but decode steps serialize under a lock.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
