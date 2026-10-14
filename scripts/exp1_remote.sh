#!/bin/bash
# Self-contained remote driver for one EXP1 arm. Runs ON the Vast instance.
# Usage: exp1_remote.sh <ARM:A|B> <CONFIG_BASENAME> [STRIP_SRC_MODEL]
set -uo pipefail
ARM="$1"; CFG="/workspace/deephat-v2/configs/$2"; STRIP_SRC="${3:-}"
export DEBIAN_FRONTEND=noninteractive
export HF_HOME=/workspace/hf
export HF_HUB_ENABLE_HF_TRANSFER=1
export TOKENIZERS_PARALLELISM=false
mkdir -p /workspace/{out,logs} "$HF_HOME"
LOG=/workspace/logs/exp1_arm${ARM}.log
exec > >(tee -a "$LOG") 2>&1
echo "[$(date -Is)] ===== EXP1 ARM $ARM START ====="
[ -f /workspace/.hf_token ] && export HF_TOKEN="$(cat /workspace/.hf_token)" && export HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"

nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# --- deps ---
apt-get update -qq && apt-get install -y -qq git curl build-essential 2>&1 | tail -2
cd /workspace
[ -d LLaMA-Factory/.git ] || git clone --depth 1 https://github.com/hiyouga/LLaMA-Factory.git LLaMA-Factory
cd LLaMA-Factory
python -m pip install -q -U pip wheel setuptools 2>&1 | tail -1
python -m pip install -q -e ".[torch,metrics,bitsandbytes]" 2>&1 | tail -3
python -m pip install -q "transformers==5.5.0" "accelerate>=1.6" "peft>=0.14" "datasets>=3.2" sentencepiece protobuf tiktoken einops hf_transfer 2>&1 | tail -3

# flash-attn: WHEEL ONLY, never compile (budget). Fall back to sdpa.
FA_OK=0
timeout 240 python -m pip install -q flash-attn --only-binary :all: 2>&1 | tail -3 && python -c "import flash_attn" 2>/dev/null && FA_OK=1
if [ "$FA_OK" != "1" ]; then
  echo "[warn] flash-attn wheel unavailable -> switching config to sdpa"
  sed -i 's/^flash_attn: fa2/flash_attn: sdpa/' "$CFG"
else
  echo "[ok] flash-attn available"
fi
python -c "import torch,transformers; print('torch',torch.__version__,'tf',transformers.__version__,'cuda',torch.cuda.get_device_name(0))"

# --- data ---
mkdir -p /workspace/LLaMA-Factory/data
cp -f /workspace/deephat-v2/data/filtered/exp1_slice_20k.jsonl /workspace/LLaMA-Factory/data/
cp -f /workspace/deephat-v2/data/filtered/dataset_info.json /workspace/LLaMA-Factory/data/dataset_info.json

# --- Arm A: strip MTP head ---
if [ -n "$STRIP_SRC" ]; then
  echo "[$(date -Is)] stripping MTP head from $STRIP_SRC"
  python /workspace/deephat-v2/scripts/strip_mtp_head.py "$STRIP_SRC" /workspace/qwen3.5-9b-base-nomtp 2>&1 | tail -5
fi

# --- peak VRAM logger ---
( while true; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; sleep 5; done ) > /workspace/logs/vram_arm${ARM}.log &
VRAMPID=$!

# --- train ---
echo "[$(date -Is)] TRAIN START arm $ARM"
cd /workspace/LLaMA-Factory
llamafactory-cli train "$CFG" 2>&1 | tee /workspace/logs/train_arm${ARM}.log
TRAIN_EXIT=${PIPESTATUS[0]}
echo "[$(date -Is)] TRAIN END arm $ARM exit=$TRAIN_EXIT"
kill $VRAMPID 2>/dev/null
PEAK=$(sort -n /workspace/logs/vram_arm${ARM}.log 2>/dev/null | tail -1)
echo "PEAK_VRAM_MB=$PEAK"

OUT=$(python -c "import yaml;print(yaml.safe_load(open('$CFG'))['output_dir'])")
echo "output_dir=$OUT"; ls -la "$OUT" 2>/dev/null

# --- refusal canary (only if adapter produced) ---
if [ "$TRAIN_EXIT" = "0" ] && [ -d "$OUT" ]; then
  echo "[$(date -Is)] CANARY START"
  cd /workspace/deephat-v2/eval
  python run_eval.py --model "$OUT" --tag arm${ARM} --out /workspace/logs/canary_arm${ARM} 2>&1 | tail -30 || echo "[warn] canary gen failed"
  python - "$ARM" <<'PY'
import json,glob,sys,os
sys.path.insert(0,'/workspace/deephat-v2/scripts')
from refusal_filter import is_refusal
arm=sys.argv[1]; d=f"/workspace/logs/canary_arm{arm}"
summ={}
for f in sorted(glob.glob(d+"/*.json")):
    rows=json.load(open(f)); name=os.path.basename(f)
    ref=sum(1 for r in rows if is_refusal(r.get('answer','')))
    summ[name]={"n":len(rows),"refusals":ref,"refusal_rate":round(ref/len(rows),3)}
json.dump(summ,open(f"/workspace/logs/canary_summary_arm{arm}.json","w"),indent=1)
print("CANARY_SUMMARY",json.dumps(summ))
PY
fi

echo "ARM${ARM}_EXIT=$TRAIN_EXIT PEAK_VRAM_MB=$PEAK" > /workspace/logs/arm${ARM}.done
echo "[$(date -Is)] ===== EXP1 ARM $ARM DONE ====="
