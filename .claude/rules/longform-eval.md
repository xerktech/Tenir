---
paths:
  - "scripts/longform_eval/**"
  - "scripts/translation_eval/public/ws_driver.py"
  - "api/src/api/stt/streaming.py"
---

# Long-form STT → translation eval

## Standing it up

- The deployed models answer from Turma hosts without auth: `tenir-stt.ai.svc.cluster.local:8000`
  (Parakeet) and `tenir-translator.ai.svc.cluster.local:8000` (vLLM, model `milmmt-46-4b`).
  The documented `maxai`/`10.10.10.x` endpoints may not be reachable; LiteLLM needs a key.
- Run the app under test locally with `uvicorn api.main:app --ws-ping-interval 0` pointed at
  those two. Creating pods in the `ai` namespace may be refused by the permission policy.
- Iterate offline (`offline_stt.py` → `replay_trigger.py` → `score.py`); a live
  `ws_driver.py` pass costs 10+ min per chunk. Turn boundaries count audio bytes, so
  offline finals match live at the production 350 ms partial cadence (the Settings
  default is 700); `--no-partials` runs lose the XERK-174 fallback.
- The shipped translators swallow errors and return None; count failures by "sent but
  None" (`replay_trigger.expects_call`), never by catching exceptions.

## Pitfalls that cost real time

- A loaded host makes inline STT decodes outlast the partial cadence; with uvicorn's
  default keepalive every 10-min session then drops with `1011 keepalive ping timeout`.
- Keep STT concurrency ≤ 3–4: the shared server stalls past the engine's 15 s timeout.
- Parallel OCR workers must not share a temp file — they OCR each other's frames
  silently (WER jumped 25% → 80% on the affected chunks).
- Parakeet ignores a pinned `language` (identical output), so pinning can't stop it
  emitting English for Spanish speech.
- Parakeet blanks some speech windows deterministically; the same audio padded with
  silence decodes. An offline sweep's per-setting differences of ~±1 WER are mostly
  which windows happen to blank — don't read them as a segmentation signal.
- The Spanish reference is OCR of a caption track: it condenses speech, so WER has a
  non-zero floor; compare runs, don't quote absolute WER as model error.
