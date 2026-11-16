# tenir-diarizer: streaming speaker diarization (NeMo Sortformer) + 2-speaker separation
# (SpeechBrain SepFormer) behind POST /v1/diarize. See README.md (XERK-2958).
#
# Plain Python base + PyPI CUDA torch rather than the NGC NeMo container: it needs no NGC
# login, and the eval pinned in requirements.txt ran on exactly these versions. The full
# (non-slim) image carries the compilers a few NeMo ASR deps build with. Multi-GB, so the
# release build runs on the self-hosted runner.
FROM python:3.12-bookworm

LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir diarizer — speaker tracking for mic captions"

WORKDIR /app

COPY diarizer/requirements.txt ./requirements.txt
# hadolint ignore=DL3013
RUN pip install --no-cache-dir -r requirements.txt

COPY diarizer/server.py diarizer/planner.py ./

# Unprivileged: the server needs nothing root owns. The deployment's fsGroup gives this
# uid the cache volume.
RUN useradd --uid 10001 --create-home diarizer
USER 10001

# Weights live in the mounted cache (the deployment's init container fetches them), so a
# restart never needs the Hub.
ENV HF_HOME=/cache/huggingface \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# One worker: the models are resident once and every request shares one GPU lock.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
