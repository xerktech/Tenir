# parakeet-stt: NVIDIA Parakeet (NeMo) behind an OpenAI /v1/audio/transcriptions
# surface, so it drops in wherever the api speaks to vLLM-Voxtral today. See
# server.py for the why (dedicated ASR = no conversational hallucinations, XERK-92).
#
# Base: the NGC NeMo container, which ships a tested torch + CUDA + NeMo-ASR stack.
# It is pinned via an ARG so the tag is trivial to bump — and it MUST be a build
# with Blackwell (sm_120) kernels, because the GPU box is a Blackwell card (the TEI
# image had to move to sm_120 kernels for the same reason). If an NGC pull needs
# auth on the runner, `docker login nvcr.io` once; the NeMo images are otherwise
# public. This is a multi-GB base, so the release build runs on the self-hosted
# runner (like vllm-stt), never a hosted one.
ARG NEMO_IMAGE=nvcr.io/nvidia/nemo:25.09
FROM ${NEMO_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir Parakeet STT (NeMo) — OpenAI /v1/audio/transcriptions"

WORKDIR /app

# hadolint ignore=DL3013 — pip pins live in requirements.txt; the base pins torch/NeMo.
COPY parakeet-stt/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY parakeet-stt/server.py ./server.py

# The multilingual, language-detecting Parakeet (Spanish + English + ~23 more).
# HF_HOME points at the mounted cache so the ~2.4 GB weights persist across
# container recreation (compose mounts the whole /root/.cache to also cover NeMo's
# own ~/.cache/torch/NeMo path, if that build caches there instead).
ENV PARAKEET_MODEL=nvidia/parakeet-tdt-0.6b-v3 \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8000

# One worker: a single resident model on one GPU, decodes serialized under a lock.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
