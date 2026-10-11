---
paths:
  - "api/src/api/stt/parakeet.py"
  - "api/tests/test_stt_backends.py"
  - "scripts/stt_eval/fleurs/**"
---

# HTTP STT engine (`api.stt.parakeet`) on Whisper (XERK-1476)

- Production STT is Whisper large-v3-turbo on vLLM behind the LiteLLM `parakeet` alias; the
  module, backend value and alias keep the Parakeet name. Parakeet stays a valid upstream.
- Request `verbose_json`: vLLM's Whisper reports the detected language only there, and that
  per-turn language beats text langid (it decides turns langid leaves `None`).
  - vLLM sends `"words": null`; read it as `body.get("words") or []`.
  - Tests: `test_stt_backends.py::test_parakeet_requests_verbose_json_for_the_detected_language`.
- Never drop the `_NON_SPEECH` filter. Whisper captions non-speech ("Thank you.", "you",
  "*sad music*") about 1.4 times a minute between turns, and vLLM gives no `no_speech_prob`;
  avg_logprob and RMS did not separate those from real short turns on FLEURS.
  - Match the WHOLE decode only, so real short replies survive.
  - Tests: `::test_whisper_non_speech_output_reads_as_blank`,
    `::test_real_short_turns_are_not_taken_for_non_speech`.
- Never pin a language server-side: with `es` pinned Whisper rendered 26 of 40 English clips in
  Spanish, and Turma dictation shares the route. Only a session's own `sourceLang` pins.
- Measure a model change with `scripts/stt_eval/fleurs/` (README there) before switching.
