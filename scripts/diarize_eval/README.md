# Speaker-tracking eval (XERK-2958)

Measures what the ticket asks for — a stable "Speaker N" per person and overlapped words
credited to the right one — on sessions where the truth is known.

Real recordings carry no speaker truth, so `build_session.py` lays LibriSpeech utterances out
like a table conversation: 4 speakers at different distances (gain + reverb), turns that
overlap by 0.5–2.5 s, short back-channels inside others' turns, room noise. Use them for the
numbers; read real recordings by hand for the rest (a "Speaker N" transcript per turn).

## Run

```bash
curl -L https://www.openslr.org/resources/12/dev-clean.tar.gz | tar xz
python build_session.py LibriSpeech/dev-clean out/s1 --speakers 4 --minutes 3 --seed 1

# diarizer locally (CPU works, ~4x slower than real time) or the deployed one
(cd ../../diarizer && pip install -r requirements.txt && uvicorn server:app --port 8790)

pip install -e ../../api soundfile
STT=http://tenir-stt.ai.svc.cluster.local:8000/v1
python diarize_run.py out/s1.wav out/s1.base.json --stt $STT                 # baseline
python diarize_run.py out/s1.wav out/s1.diar.json --stt $STT \
    --diarizer http://127.0.0.1:8790 --diarizer-timeout 300
python diarize_score.py out/s1.json out/s1.base.json out/s1.diar.json
```

`diarize_run.py` drives the installed api's `StreamingTranscriber`, so turn cutting, piece
decoding and fallbacks are the shipped code. Raise `--diarizer-timeout` for a CPU diarizer
(the api's default is 3 s).

## Reading the score

- `recall` — reference words in any caption; the transcript's coverage. Must not fall
  below the baseline's: splitting a turn must not lose words.
- `attributed` — reference words in captions labelled with the right speaker (labels map
  to true speakers one-to-one by matched words). The ticket's number.
- `purity` — share of matched words under the right label.
- `overlapped` / `solo` — the same split by whether the utterance overlaps another speaker.
- `label_share_per_speaker` — how a true speaker's words spread over labels; one label near
  1.0 per speaker is consistent numbering.

Results: `RESULTS-2026-11.md`.
