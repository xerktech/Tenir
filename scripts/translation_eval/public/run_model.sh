#!/bin/bash
# run_model.sh <tag> <hf_model> <native_mode|none> [extra vllm serve args...]
#
# One candidate, end to end, inside the mt-eval pod (see README.md):
#   1. vllm serve with a FIXED 2 GiB KV cache, so VRAM is comparable across models;
#      after warm-up, snapshot nvidia-smi and sample it every 500 ms (peak).
#   2. Text replay through the shim: the shipped Tenir prompt (fixed subset unless
#      FULL_SHIPPED=1) and the model's native prompt (full set).
#   3. The better mode (chrF on the common items) drives the end-to-end run: FLEURS
#      clips streamed through the live Tenir API container, which calls the shim.
set -u
TAG=$1; MODEL=$2; NATIVE=$3; shift 3
W=/work
R=$W/results/$TAG
mkdir -p "$R"
TENIR_USERNAME=evaluser TENIR_PASSWORD=$(cat "$W/eval_pw")
export TENIR_USERNAME TENIR_PASSWORD
cd "$W" || exit 1

vllm serve "$MODEL" --served-model-name "$TAG" --port 8000 --max-model-len 4096 \
  --max-num-seqs 8 --kv-cache-memory-bytes 2147483648 --generation-config vllm "$@" \
  > "$R/vllm.log" 2>&1 &
VPID=$!
for _ in $(seq 1 240); do
  curl -sf localhost:8000/v1/models >/dev/null && break
  kill -0 "$VPID" 2>/dev/null || break   # died on startup: don't wait out the timeout
  sleep 5
done
curl -sf localhost:8000/v1/models >/dev/null || {
  echo "$TAG: vllm failed"; tail -30 "$R/vllm.log"; kill "$VPID" 2>/dev/null; exit 1; }

for _ in 1 2 3; do
  curl -s localhost:8000/v1/completions -H 'content-type: application/json' \
    -d "{\"model\":\"$TAG\",\"prompt\":\"Hola\",\"max_tokens\":8}" >/dev/null
done
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader > "$R/vram_gpu.txt"
nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader > "$R/vram_procs.txt"
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -lms 500 > "$R/vram_trace.txt" &
SMI=$!
grep -E "Model loading took|KV cache size|Graph capturing finished" "$R/vllm.log" > "$R/vram_log.txt"

start_shim() {  # <mode>
  # "[u]vicorn" so the pattern never matches this shell's own command line
  pkill -f "[u]vicorn shim:app" 2>/dev/null; sleep 1
  UPSTREAM=http://127.0.0.1:8000/v1 SERVED_MODEL=$TAG MODE=$1 nohup python3 -m uvicorn shim:app \
    --host 127.0.0.1 --port 9000 --log-level warning > "$R/shim_$1.log" 2>&1 &
  for _ in $(seq 1 30); do curl -s -o /dev/null localhost:9000/docs && break; sleep 1; done
}

modes="shipped"
[ "$NATIVE" != none ] && modes="shipped $NATIVE"
for m in $modes; do
  start_shim "$m"
  sub=()
  [ "$m" = shipped ] && [ "${FULL_SHIPPED:-0}" != 1 ] && sub=(--subset)
  python3 replay_text.py data/items_all.json "${sub[@]}" --out "$R/text_$m.json" | tail -1
done

# shellcheck disable=SC2086  # $modes is a deliberate word list
BEST=$("$W/venv/bin/python" - "$R" $modes <<'PY'
import json, sys, sacrebleu
R, modes = sys.argv[1], sys.argv[2:]
runs = {m: {x["id"]: x for x in json.load(open(f"{R}/text_{m}.json"))} for m in modes}
common = set.intersection(*(set(v) for v in runs.values()))
def chrf(m):
    d = [runs[m][i] for i in sorted(common) if runs[m][i]["set"] != "passthrough"]
    return sacrebleu.corpus_chrf([x.get("hyp") or "" for x in d], [[x["ref"] for x in d]]).score
s = {m: chrf(m) for m in modes}; print(max(s, key=s.get)); print(s, file=sys.stderr)
PY
)
echo "$TAG best mode: $BEST"; echo "$BEST" > "$R/best_mode"
start_shim "$BEST"
python3 ws_driver.py data/clips.json --out "$R/e2e_$BEST.json" --concurrency 6 | tail -1
pkill -f "[u]vicorn shim:app"
kill "$SMI" "$VPID"; wait "$VPID" 2>/dev/null; sleep 5
echo "$TAG DONE"
