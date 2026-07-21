# vllm-stt image for Voxtral: the stock vllm/vllm-openai image lacks the audio
# decoding deps Voxtral needs (mistral_common's soundfile path), so it crashes at
# engine init with "soundfile is not installed". Add libsndfile + soundfile here.
#
# PyAV (`av`) is also required: vLLM's load_audio reads the WAV with soundfile but
# resamples any non-16 kHz input to the model rate via resample_audio_pyav(), which
# imports `av`. Without it, only 16 kHz audio decodes — anything else 400s with
# "Invalid or unsupported audio file." Our api always sends 16 kHz so live STT works,
# but off-rate callers (e.g. LiteLLM's 22.05 kHz health-check clip, real-world uploads)
# fail. Installing PyAV makes STT robust to any input sample rate.
FROM vllm/vllm-openai:latest
# Auto-link the GHCR package to this repo so the Actions token inherits write
# access on push (see api/Dockerfile.sidecar for the full rationale).
LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir vLLM STT (Voxtral) with audio-decoding deps"
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "mistral_common[soundfile]" soundfile av
# Enable pinned host memory on WSL2. Voxtral is served by vLLM's V2 GPU model
# runner, whose RequestState allocates a UvaBuffer (Unified Virtual Addressing)
# at engine init. UVA requires pinned memory, and vLLM disables pinned memory by
# default under WSL2 (even on modern kernels), so init aborts with
# "RuntimeError: UVA is not available" and the container crash-loops. This flag is
# vLLM's documented opt-in to re-enable pinned memory on WSL2 kernels that support
# it; it is a no-op on non-WSL hosts (pinned memory is already available there).
# The co-tenant cue LLM (Qwen) escapes this because it uses the V1 runner, which
# never touches UvaBuffer — so only this Voxtral image needs the flag.
ENV VLLM_WSL2_ENABLE_PIN_MEMORY=1
