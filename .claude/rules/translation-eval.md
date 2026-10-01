---
paths:
  - "scripts/translation_eval/**"
  - "api/src/api/translate/**"
---

# Translation model evals

## Serving candidates (vLLM 0.30, RTX PRO 6000 / sm_120)

- `vllm/vllm-openai:v0.30.0-cu129` ships torch 2.14 (cu130) over a cu129 torchvision, so
  `vllm` won't import (`torchvision::nms does not exist`). Pin `torch==2.13.0` from the
  cu129 index (`public/setup.sh`). The cu129 *nightly* image is worse: missing `libnvrtc.so.13`.
- Never `pip install` scorers (unbabel-comet) into the vLLM image's site-packages: it
  silently downgrades numpy and transformers (5.x → 4.57) and Hy-MT2 then won't load.
  Use a separate venv.
- Hy-MT2 (`HunYuanDenseV1ForCausalLM`) has no native vLLM 0.30 impl; the Transformers
  backend's "dynamic" RoPE wrapper fails CUDA-graph capture (`operation not permitted when
  stream is capturing`). `--hf-overrides` to `rope_type: default` with
  `rope_theta = 1e4 * 1000**(128/126)` is exact below 262K tokens (verified 40/40
  token-identical); `--enforce-eager` instead is ~2.8× slower and unfair in a latency race.
- The ungated `Infomaniak-AI/vllm-translategemma-*` repack has a flattened
  `rope_parameters` that transformers 5.17 rejects (`'float' object has no attribute
  'get'`); rename it back to `rope_scaling` in the local snapshot (`public/run_all.sh`).
- VRAM comparisons need `--kv-cache-memory-bytes` fixed across models; the default
  `gpu_memory_utilization` grabs ~90% of the card and every model "uses" the same amount.

## Prompts / product contract

- Dedicated MT models do not follow the shipped JSON-envelope prompt: MiLMMT rambles to
  `max_tokens`, TranslateGemma's template 400s. Only Hy-MT2 survives it. A model swap
  means a native-prompt adapter in the translator (`public/shim.py` is a reference).
- Source-language prompts (MiLMMT, TranslateGemma) inherit Tenir's langid errors: short
  Spanish turns tagged pt/fr get translated "from Portuguese". Hy-MT2's prompt has no
  source language and is immune.
- Told English is Spanish, MT models paraphrase it; skip the call for English text rather
  than claiming a source language (graded by the `passthrough` set).

## Running in the cluster

- Long `kubectl exec` commands drop (`websocket: close 1006`); run work with
  `setsid nohup … < /dev/null &` and poll a log.
- `pkill -f "<pattern>"` inside `kubectl exec bash -c '…'` kills the exec'ing shell when the
  pattern appears in its own command line; use the `[v]llm serve` bracket trick.
- talos04 node DNS intermittently fails Docker Hub (`lookup auth.docker.io … server
  misbehaving`); kubelet retries succeed, it is not an auth problem.
- A WS client streaming FLEURS must not stop at "1.5 s after audio": late finals arrive
  well after; finals that never arrive are XERK-1349, not the harness.
