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
  `rope_theta = 1e4 * 1000**(128/126)` is exact below 262K tokens (eager stock vs eager
  override: 40/40 token-identical). Served with CUDA graphs it matches stock on 35/40 —
  ordinary kernel drift, not the override. `--enforce-eager` is ~2.8× slower.
- The ungated `Infomaniak-AI/vllm-translategemma-*` repack has a flattened
  `rope_parameters` that transformers 5.17 rejects (`'float' object has no attribute
  'get'`); rename it back to `rope_scaling` in the local snapshot (`public/run_all.sh`).
- VRAM comparisons need `--kv-cache-memory-bytes` fixed across models; the default
  `gpu_memory_utilization` grabs ~90% of the card and every model "uses" the same amount.

## Prompts / product contract

- Dedicated MT models do not follow the shipped JSON-envelope prompt: MiLMMT-1B rambles to
  `max_tokens`, the 4B/12B mostly answer `{}`, TranslateGemma's template 400s. Only Hy-MT2
  (1.8B and 7B) survives it. Otherwise a swap needs a native-prompt mode
  (`API_TRANSLATION_PROMPT_STYLE`, `api/src/api/translate/completion.py`).
- Source-language prompts can inherit Tenir's langid errors: a Spanish turn tagged pt
  gets translated "from Portuguese" (MiLMMT loses ~1 COMET on those clips). It is not
  universal — TranslateGemma gained on the same clips — so measure, don't assume.
- Told English is Spanish, MT models paraphrase it. The app already never sends
  `en`-tagged turns; the `passthrough` set mostly measures a path production doesn't take
  (langid tags 145/150 of its sentences `en`), so weight it accordingly.

## Running in the cluster

- Long `kubectl exec` commands drop (`websocket: close 1006`); run work with
  `setsid nohup … < /dev/null &` and poll a log.
- `pkill -f "<pattern>"` inside `kubectl exec bash -c '…'` kills the exec'ing shell when the
  pattern appears in its own command line; use the `[v]llm serve` bracket trick.
- talos04 node DNS intermittently fails Docker Hub (`lookup auth.docker.io … server
  misbehaving`); kubelet retries succeed, it is not an auth problem.
- A WS client capturing STT must wait a fixed ~20 s per clip: early-stop rules (1.5 s,
  "6 s after the last final") lost finals on up to 23/200 clips that the e2e runs captured.
  A "missing tail" can be trailing silence in the recording — check the reference text.
- Report per-set deltas, not just the pooled one: OPUS-100 (noisy refs) can carry a pooled
  "win" that the FLEURS speech sets don't show (it did in the Oct 2026 round).
