#!/bin/bash
# The Oct 2026 sweep, smallest first, after setup.sh + the STT capture (README.md).
cd /work || exit 1
until grep -q STT_OK stt.log; do sleep 20; done
python3 build_asr_items.py

# Hy-MT2 on vLLM 0.30 only runs via the Transformers backend, whose "dynamic" RoPE wrapper
# breaks CUDA-graph capture. Below 262K tokens it never changes the frequencies, so the
# same static base (theta * alpha^(d/(d-2)) = 1e4 * 1000^(128/126)) as rope_type "default"
# is exact — verified token-identical (eager stock vs eager override, 40/40).
ROPE='{"rope_parameters": {"rope_type": "default", "rope_theta": 11158839.92507748}}'

# The ungated Infomaniak TranslateGemma repack flattened rope_parameters for an older vLLM;
# transformers 5.17 rejects that shape. Restore the legacy rope_scaling key (same values as
# every Gemma3 config, e.g. MiLMMT's) in the local snapshot.
for m in Infomaniak-AI--vllm-translategemma-4b-it Infomaniak-AI--vllm-translategemma-12b-it; do
  f=$(ls /models/hf/hub/models--$m/snapshots/*/config.json)
  python3 - "$f" <<'PY'
import json, os, sys
f = sys.argv[1]; c = json.load(open(f)); t = c.get("text_config", c)
if "rope_parameters" in t:
    t["rope_scaling"] = t.pop("rope_parameters")
    os.remove(f)  # replace the HF-cache symlink, leave the blob intact
    json.dump(c, open(f, "w"), indent=2)
PY
done

bash run_model.sh milmmt-1b  xiaomi-research/MiLMMT-46-1B-v1.0       milmmt
bash run_model.sh hymt2-1.8b tencent/Hy-MT2-1.8B                     hymt   --hf-overrides "$ROPE"
bash run_model.sh milmmt-4b  xiaomi-research/MiLMMT-46-4B-v1.0       milmmt --language-model-only
bash run_model.sh tgemma-4b  Infomaniak-AI/vllm-translategemma-4b-it  tgemma --language-model-only
bash run_model.sh hymt2-7b   tencent/Hy-MT2-7B                       hymt   --hf-overrides "$ROPE"
bash run_model.sh milmmt-12b xiaomi-research/MiLMMT-46-12B-v1.0      milmmt --language-model-only
bash run_model.sh tgemma-12b Infomaniak-AI/vllm-translategemma-12b-it tgemma --language-model-only
# Baseline: production's weights family on the same card. FP8 on vLLM — prod ran NVFP4 +
# DFlash on SGLang (ai/tenir/qwen.yaml in ArgoCD). Qwen3.8 is hybrid (~2 GB recurrent state
# per concurrent request), so it gets 2 seqs and 8 GiB instead of 8 seqs / 2 GiB.
bash run_model.sh qwen3.8-27b-fp8 Qwen/Qwen3.8-27B none --language-model-only --quantization fp8 \
  --max-num-seqs 2 --kv-cache-memory-bytes 8589934592
echo ALL_DONE
