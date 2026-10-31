# tenir-ocr: PaddleOCR PP-OCRv5 mobile on CPU behind POST /v1/ocr (see server.py, README.md).
# Build context is the repo root (the release pipeline's convention).
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir OCR (PaddleOCR PP-OCRv5 mobile, CPU) for meeting captions"

# libgomp1: Paddle's OpenMP runtime. libgl1/libglib2.0-0: opencv-contrib-python, which paddlex
# pins (the non-headless build), imports them at load time.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ocr/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Models are baked in, so the pod needs no egress and a model-host outage cannot stop a
# restart. paddlex also writes locks/temp under its cache dir, so the runtime user owns it.
ENV PADDLE_PDX_CACHE_HOME=/opt/paddlex \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    OMP_NUM_THREADS=1 \
    PYTHONUNBUFFERED=1
COPY ocr/server.py ./server.py
RUN useradd --uid 10001 --no-create-home --home-dir /nonexistent ocr \
    && python -c "import server; server._init_worker()" \
    && chown -R 10001:10001 /opt/paddlex
USER 10001

EXPOSE 8000
# One uvicorn process; server.py runs OCR_WORKERS engine processes behind it.
CMD ["python", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
