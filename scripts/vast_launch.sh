#!/bin/bash
# vast_launch.sh — PLACEHOLDER wrapper around the Vast.ai API to rent a GPU and
# kick off one EXP arm. Reads VAST_API_KEY from env. Does NOT spend money on its
# own: it stops at a dry-run plan unless you pass --confirm, and even then a
# human must have approved the spend (see reports/TEAM_SYNC.md: Vast spend needs
# Derek's yes via the Lead). NEVER prints the API key.
#
# Target GPUs:
#   - A100 80GB  for 9B bf16 LoRA  (~$1/hr)
#   - RTX 4090   for 14B QLoRA (insurance arm)
#
# Usage:
#   VAST_API_KEY=... ./vast_launch.sh --arm A --gpu A100 --image pytorch/pytorch
#   VAST_API_KEY=... ./vast_launch.sh --arm A --gpu A100 --confirm   # actually rent
set -uo pipefail

: "${VAST_API_KEY:?set VAST_API_KEY in env (never commit it)}"
API="https://console.vast.ai/api/v0"

ARM="A"; GPU="A100"; IMAGE="pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel"; CONFIRM=0
DISK=120; MAXPRICE="1.5"
while [ $# -gt 0 ]; do
  case "$1" in
    --arm)    ARM="$2"; shift 2;;
    --gpu)    GPU="$2"; shift 2;;
    --image)  IMAGE="$2"; shift 2;;
    --disk)   DISK="$2"; shift 2;;
    --max)    MAXPRICE="$2"; shift 2;;
    --confirm) CONFIRM=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

auth=(-H "Authorization: Bearer ${VAST_API_KEY}")

# 1) search cheapest matching offer
QUERY="reliability>0.98 num_gpus=1 gpu_name=${GPU} dph_total<${MAXPRICE} rentable=true"
echo "[vast] searching offers: ${QUERY}"
OFFER=$(curl -s "${auth[@]}" --data-urlencode "q=${QUERY}" "${API}/bundles/" \
  | python3 -c "import sys,json; o=json.load(sys.stdin).get('offers',[]); o.sort(key=lambda x:x.get('dph_total',9e9)); print(o[0]['id'], round(o[0]['dph_total'],3)) if o else print('NONE 0')")
OID=$(echo "$OFFER" | awk '{print $1}')
DPH=$(echo "$OFFER" | awk '{print $2}')
if [ "$OID" = "NONE" ]; then echo "[vast] no matching offer"; exit 1; fi
echo "[vast] cheapest offer id=${OID} dph_total=\$${DPH}/hr  arm=${ARM} gpu=${GPU}"

ONSTART="bash -lc 'cd /workspace && git clone <THIS_REPO> cgr && cd cgr && ./scripts/exp1_remote.sh ${ARM} <CONFIG_BASENAME>'"

if [ "$CONFIRM" != "1" ]; then
  cat <<PLAN
[vast] DRY RUN — no instance created. Would POST ${API}/asks/${OID}/ with:
       image=${IMAGE} disk=${DISK}GB onstart=${ONSTART}
       est. cost \$${DPH}/hr. Re-run with --confirm AND documented human approval to rent.
PLAN
  exit 0
fi

echo "[vast] creating instance from offer ${OID} (spend ~\$${DPH}/hr)…"
curl -s "${auth[@]}" -H "Content-Type: application/json" \
  -d "$(python3 -c "import json,os,sys; print(json.dumps({'client_id':'me','image':os.environ.get('IMAGE','$IMAGE'),'disk':$DISK,'onstart':'''$ONSTART'''}))")" \
  "${API}/asks/${OID}/" | python3 -m json.tool
echo "[vast] done. Remember to destroy the instance when training ends (see scripts/watch_exp1.sh)."
