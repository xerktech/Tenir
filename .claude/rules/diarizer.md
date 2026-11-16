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
- `SortformerStream` must stay equivalent to NeMo's whole-file `forward_streaming`. It commits
  a chunk only with its full right context, and scores the tail on a deep copy of the state.
  Feature frames come from audio with 2 frames of margin (center=True, n_fft 512).
  - Re-check after a NeMo bump: feed a recording turn by turn, compare against
    `model.diarize(...)` probs (was max abs diff 1.3e-6, Nov 2026).
- Sortformer tracks at most 4 speakers; a fifth folds into one of the four. Don't add an
  embedding re-clusterer to "fix" it without a measured win on `scripts/diarize_eval`.
- Real recordings have no speaker truth: measure on `scripts/diarize_eval` synthetic sessions,
  then read a real one by hand. Never put recording audio or transcripts in the repo.
- CPU runs ~2x slower than real time on Sortformer alone and far slower with SepFormer; a CPU
  diarizer needs `--diarizer-timeout` in the hundreds of seconds.
