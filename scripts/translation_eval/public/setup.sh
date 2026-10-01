#!/bin/bash
# In-pod setup for the mt-eval pod (README.md): data, scorer venv, torch pin.
set -e
mkdir -p /work/data /work/results /work/smoke
cd /work/data
H=https://huggingface.co/datasets
curl -sfL -o es_test.tsv "$H/google/fleurs/resolve/main/data/es_419/test.tsv"
curl -sfL -o en_test.tsv "$H/google/fleurs/resolve/main/data/en_us/test.tsv"
curl -sfL -o en_dev.tsv "$H/google/fleurs/resolve/main/data/en_us/dev.tsv"
curl -sfL "$H/google/fleurs/resolve/main/data/es_419/audio/test.tar.gz" | tar xz

# vllm/vllm-openai:v0.30.0-cu129 ships torch 2.14.0 (a cu130 build) over the cu129
# torchvision, so `vllm` fails to import (torchvision::nms missing). vLLM 0.30.0 pins
# torch==2.13.0; reinstall exactly that from the cu129 index.
pip install -q --no-deps torch==2.13.0 --index-url https://download.pytorch.org/whl/cu129

# Scorers live in their own venv: unbabel-comet downgrades numpy and transformers,
# which breaks vLLM if installed into the system site-packages.
python3 -m venv /work/venv
/work/venv/bin/pip install --retries 10 --timeout 60 -q sacrebleu unbabel-comet pandas pyarrow \
  "setuptools<81"  # torchmetrics still imports pkg_resources

/work/venv/bin/python /work/prep.py
python3 /work/conv.py
echo SETUP_OK
