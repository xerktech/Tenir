---
paths:
  - diarizer/**
  - api/src/api/stt/diarize.py
  - api/src/api/stt/streaming.py
  - api/tests/test_diarize.py
  - scripts/diarize_eval/**
---

# Speaker tracking (XERK-2958)

- Off unless `API_DIARIZER_URL` is set; every failure path captions the turn whole and unlabelled.
  Never let a diarizer error drop or delay a turn past `API_DIARIZER_TIMEOUT_SECONDS`.
- Every closed turn goes to the diarizer, silent ones too: the service's streaming state is a
  timeline, and a skipped turn shifts its speaker cache against the audio.
  - Tests: `test_diarize.py::test_every_turn_reaches_the_diarizer_on_one_timeline`.
- The session key is the conversation id, so a resume keeps numbering while the pod is up. A
  diarizer restart restarts numbering for live conversations — accepted, not a bug.
  - A new sitting (`epoch`) whose offset is behind the session's timeline restarts the session:
    the api resumed with no retained audio. Skipping that audio as "already seen" mislabelled it.
  - Tests: `diarizer/tests/test_server.py::test_a_new_sitting_behind_the_timeline_restarts_the_session`.
- After a diarizer failure the api skips it for `_COOLDOWN_S`; never retry per turn — every
  turn waits on the diarizer, so a hung one stalled captions 27 s within 20 turns (QA).
- A sequential piece tiles only up to the mid-pause before the next interval of ANY speaker,
  overlapped ones included; tiling against sequential neighbours gave a lone solo stretch the
  whole turn (QA). Tests: `test_planner.py::test_a_solo_stretch_stops_where_an_overlap_begins`.
- Never run the model over a long gap of zeros: jumps over `MAX_GAP_S` move the clock.
- A one-piece turn must decode exactly as without the diarizer (one whole-turn decode). Only
  multi-speaker turns split, and a split whose pieces all decode blank falls back to the whole turn.
  - Tests: `::test_a_single_speaker_turn_is_decoded_whole_and_labelled`,
    `::test_a_split_whose_pieces_all_come_back_blank_captions_the_turn_whole`.
- Separated pieces carry no word timing (same span, different samples) and are de-duplicated by
  word overlap: SepFormer sometimes leaves one voice in both streams.
- A separated piece Whisper decodes below `_MIN_SEPARATED_LOGPROB` (avg_logprob −0.5) is
  dropped, not replaced by the mix: SepFormer artefacts decode as fluent lines nobody said
  (XERK-3006). Retune it on p1 + s1/s3 (`RESULTS-2026-11.md`), never on s1 alone (it separates
  nothing). Raw-span pieces are never gated.
  - Tests: `test_diarize.py::test_a_separated_stream_whisper_is_unsure_of_is_not_captioned`.
  - One threshold for every language, on purpose (XERK-3069). Spanish separated decodes are
    right more often at a given avg_logprob, but below −0.5 still under 50 %, so a lower `es`
    threshold lost precision and recall on real voices. Re-measure with
    `diarize_gate.py replay --lang-gate es=T` before keying it on language.
  - A gate replay must model the whole-turn caption when no piece of a turn emits
    (`_finalize_pieces` returns False); without it the shipped gate scored recall 0.03, not 0.26.
- The separator stays SepFormer whamr16k (XERK-3071). MossFormer2 separates real voices better
  but behind the −0.5 gate it scored no better on the dinner mixtures. Compare a candidate with
  `diarize_sepmix.py --separator X --out-name F` + `replay --truth`, at the shipped gate AND a
  retuned one, with a paired bootstrap over mix dirs: 49 mixtures can't resolve ±0.05.
- `SortformerStream` must stay equivalent to NeMo's whole-file `forward_streaming`. It commits
  a chunk only with its full right context, and scores the tail on a deep copy of the state.
  Feature frames come from audio with 2 frames of margin (center=True, n_fft 512).
  - Re-check after a NeMo bump: feed a recording turn by turn, compare against
    `model.diarize(...)` probs (was max abs diff 1.3e-6, Nov 2026).
- Sortformer has 4 slots; `fingerprint.py` maps slots to an unbounded set of people (XERK-3017).
  - Thresholds were tuned jointly on 4/6/8-speaker sessions. Lower `MATCH_COS` merged two
    people of a 4-speaker table; re-tune on all of them, never one, and hold out fresh seeds.
  - Fingerprints stay optional: a TitaNet load failure must leave 4-slot tracking, not a down
    service. Tests: `test_server.py::test_a_fingerprint_model_that_fails_to_load_leaves_4_slot_tracking`.
- Real recordings have no speaker truth: measure on `scripts/diarize_eval` synthetic sessions,
  then read a real one by hand. Never put recording audio or transcripts in the repo.
- CPU runs ~2x slower than real time on Sortformer alone and far slower with SepFormer; a CPU
  diarizer needs `--diarizer-timeout` in the hundreds of seconds.
- `/v1/diarize` fails closed: no `DIAR_API_KEY` → 503, wrong bearer → 401, both before any
  session work (XERK-3005). The NetworkPolicy is a second layer; never make the key optional.
  - Both sides `.strip()` the key: a secret written with `echo` ends in a newline no header carries.
  - Tests: `test_server.py::test_without_a_configured_key_every_turn_is_refused`.
- An abandoned turn (api hung up) is dropped: checked after taking the session lock and between
  model steps, via `anyio.from_thread.run(request.is_disconnected)`. Raise `Abandoned` only
  BEFORE a `_step`, so stream state stays consistent and the next feed scores the backlog.
  - Tests: `::test_abandoning_mid_feed_leaves_the_stream_resumable`,
    `::test_every_feed_of_a_turn_can_be_abandoned_between_steps`.
