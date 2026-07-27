# nemotron-stt: NVIDIA Nemotron cache-aware streaming ASR (NeMo) behind a WebSocket
# /v1/audio/stream surface. It serves the LIVE-PARTIAL half of the hybrid STT path;
# the offline Parakeet server keeps the FINAL. See server.py for the why, and
# docs/stt-model-gpu-benchmark.md for the GPU benchmark that motivated it.
#
# Base: the NGC NeMo container (tested torch + CUDA + NeMo-ASR). Pinned via ARG so
# the tag is trivial to bump — and it MUST carry Blackwell (sm_120) kernels, because
# the GPU box is a Blackwell card. Multi-GB base → the release build runs on the
# self-hosted runner (like parakeet-stt / vllm-stt), never a hosted one.
ARG NEMO_IMAGE=nvcr.io/nvidia/nemo:25.09
FROM ${NEMO_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir Nemotron streaming STT (NeMo) — WebSocket /v1/audio/stream"

WORKDIR /app

# nemotron-3.5-asr-streaming needs a NeMo new enough to know its model class
# (EncDecRNNTBPEModelWithPrompt / rnnt_bpe_models_prompt). The NGC 25.09 base ships
# NeMo 2.5.x, which predates it, so from_pretrained fails with
# "No module named 'nemo.collections.asr.models.rnnt_bpe_models_prompt'" and the
# server never leaves /health 503. The base installs NeMo as an EDITABLE checkout at
# /opt/NeMo, so we fast-forward that checkout to a commit that has the class and add
# the one new runtime dep it pulls in (kaldialign) — reusing the base's Blackwell-tuned
# torch/CUDA rather than dragging in a fresh torch. Verified on the GPU host: model
# loads + streaming-decodes. Bump NEMO_COMMIT when moving to a newer model/NeMo.
# hadolint ignore=DL3013
ARG NEMO_COMMIT=fb20f062f0c263ee3bdde6decfd0ec1f5df122ed
RUN cd /opt/NeMo \
 && git fetch --depth 1 origin "${NEMO_COMMIT}" \
 && git checkout FETCH_HEAD \
 && pip install --no-cache-dir kaldialign

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

# server.py's main() loads the model on the main thread BEFORE starting uvicorn, then
# serves the WebSocket with wsproto (server.py WS_IMPL). Loading before the loop keeps
# the accept path healthy through NeMo's cold CUDA init; wsproto keeps the handshake
# off the `websockets` library, whose version churn broke it twice (a 400 on uvicorn's
# legacy impl, then a silent hang on websockets-sansio in production). See _load_model
# / main() / WS_IMPL for the full rationale. One worker: a single resident model on one
# GPU.
CMD ["python", "server.py"]
