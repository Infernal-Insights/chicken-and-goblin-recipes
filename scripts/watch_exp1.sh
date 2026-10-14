#!/bin/bash
# Self-contained EXP1 watcher: polls both arms, pulls artifacts, destroys instances.
set -uo pipefail
SSHA="ssh -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p 10954 root@192.165.134.28"
SSHB="ssh -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p 44279 root@38.117.87.38"
SCPA="scp -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=20 -P 10954"
SCPB="scp -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o ConnectTimeout=20 -P 44279"
OUTA=/workspace/deephat-v2/outputs/exp1_armA
OUTB=/workspace/deephat-v2/outputs/exp1_armB
STATE=/workspace/deephat-v2/reports/exp1_watch_state.json
LOG=/workspace/deephat-v2/outputs/exp1_watch.log
mkdir -p "$OUTA" "$OUTB" "$(dirname "$STATE")"
IDA=55937320
IDB=55939212
DONEA=0; DONEB=0
POLL=0
log(){ echo "$(date -Is) $*" | tee -a "$LOG"; }
destroy(){
  local id="$1"
  log "DESTROY $id"
  curl -s -X DELETE -H "Authorization: Bearer $VAST_API_KEY" \
    "https://console.vast.ai/api/v0/instances/$id/" | tee -a "$LOG"
  echo | tee -a "$LOG"
}
pull_arm(){
  local arm="$1" sshc="$2" scpc="$3" out="$4" host="$5"
  log "PULL arm $arm"
  mkdir -p "$out/checkpoint" "$out/logs"
  $sshc "ls -la /workspace/out/exp1_arm${arm}/; ls /workspace/logs/" >>"$LOG" 2>&1 || true
  # adapter / checkpoint
  $scpc -r "root@${host}:/workspace/out/exp1_arm${arm}/." "$out/" >>"$LOG" 2>&1 || true
  $scpc "root@${host}:/workspace/logs/train_arm${arm}.log" "$out/logs/" >>"$LOG" 2>&1 || true
  $scpc "root@${host}:/workspace/logs/exp1_arm${arm}.log" "$out/logs/" >>"$LOG" 2>&1 || true
  $scpc "root@${host}:/workspace/logs/vram_arm${arm}.log" "$out/logs/" >>"$LOG" 2>&1 || true
  $scpc -r "root@${host}:/workspace/logs/canary_arm${arm}" "$out/logs/" >>"$LOG" 2>&1 || true
  $scpc "root@${host}:/workspace/logs/canary_arm${arm}.json" "$out/logs/" >>"$LOG" 2>&1 || true
  # extract summary lines
  $sshc "grep -E 'PEAK_VRAM|TRAIN END|loss|refusal|CANARY|{\"loss\"' /workspace/logs/exp1_arm${arm}.log /workspace/logs/train_arm${arm}.log 2>/dev/null | tail -80" > "$out/summary_tail.txt" 2>&1 || true
  log "PULL arm $arm done; files=$(find "$out" -type f | wc -l)"
}
check_done(){
  local sshc="$1" arm="$2"
  # done if remote marker or no train process and output exists
  local r
  r=$($sshc "if grep -q 'TRAIN END arm $arm' /workspace/logs/exp1_arm${arm}.log 2>/dev/null; then echo DONE; elif ! pgrep -f 'llamafactory-cli train' >/dev/null && [ -f /workspace/out/exp1_arm${arm}/adapter_model.safetensors -o -f /workspace/out/exp1_arm${arm}/trainer_log.jsonl ]; then echo DONE; else
    # progress
    TAIL=\$(tail -c 400 /workspace/logs/train_arm${arm}.log 2>/dev/null | tr '\\r' '\\n' | grep -E '[0-9]+%\\|.*[0-9]+/[0-9]+' | tail -1)
    LOSS=\$(grep -oE '\"loss\":[0-9.]+' /workspace/out/exp1_arm${arm}/trainer_log.jsonl 2>/dev/null | tail -1)
    echo RUNNING:\${TAIL:-?}:\${LOSS:-}
  fi" 2>/dev/null) || r="SSH_FAIL"
  echo "$r"
}
log "watcher start DONEA=$DONEA DONEB=$DONEB"
while [ "$DONEA" != "1" ] || [ "$DONEB" != "1" ]; do
  POLL=$((POLL+1))
  if [ "$DONEA" != "1" ]; then
    SA=$(check_done "$SSHA" A)
    log "poll#$POLL armA: $SA"
    if echo "$SA" | grep -q '^DONE'; then
      # wait a bit for canary to finish if still running
      for i in 1 2 3 4 5 6; do
        CA=$($SSHA "grep -q '===== EXP1 ARM A COMPLETE' /workspace/logs/exp1_armA.log 2>/dev/null && echo COMPLETE || (pgrep -af exp1_remote|grep -v grep|head -1 || echo idle)" 2>/dev/null || echo SSH_FAIL)
        log "armA post-train: $CA"
        echo "$CA" | grep -qE 'COMPLETE|idle' && break
        sleep 30
      done
      pull_arm A "$SSHA" "$SCPA" "$OUTA" "192.165.134.28"
      destroy "$IDA"
      DONEA=1
    fi
  fi
  if [ "$DONEB" != "1" ]; then
    SB=$(check_done "$SSHB" B)
    log "poll#$POLL armB: $SB"
    if echo "$SB" | grep -q '^DONE'; then
      for i in 1 2 3 4 5 6; do
        CB=$($SSHB "grep -q '===== EXP1 ARM B COMPLETE' /workspace/logs/exp1_armB.log 2>/dev/null && echo COMPLETE || (pgrep -af exp1_remote|grep -v grep|head -1 || echo idle)" 2>/dev/null || echo SSH_FAIL)
        log "armB post-train: $CB"
        echo "$CB" | grep -qE 'COMPLETE|idle' && break
        sleep 30
      done
      pull_arm B "$SSHB" "$SCPB" "$OUTB" "38.117.87.38"
      destroy "$IDB"
      DONEB=1
    fi
  fi
  python3 - <<PY
import json
json.dump({"poll":$POLL,"doneA":$DONEA,"doneB":$DONEB,"ts":"$(date -Is)"}, open("$STATE","w"))
PY
  if [ "$DONEA" = "1" ] && [ "$DONEB" = "1" ]; then break; fi
  sleep 60
done
# verify zero
LEFT=$(curl -s -H "Authorization: Bearer $VAST_API_KEY" "https://console.vast.ai/api/v0/instances/" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('instances',[])))")
log "instances_left=$LEFT"
# write results stub
python3 - <<'PY'
import json,re,glob,os,pathlib
base=pathlib.Path("/workspace/deephat-v2/outputs")
report=[]
for arm,dph in [("A",1.11),("B",0.428)]:
    out=base/f"exp1_arm{arm}"
    losses=[]
    tlog=out/"trainer_log.jsonl"
    if not tlog.exists():
        # maybe nested
        cands=list(out.rglob("trainer_log.jsonl"))
        tlog=cands[0] if cands else tlog
    if tlog.exists():
        for line in open(tlog):
            try:
                j=json.loads(line)
                if "loss" in j: losses.append((j.get("current_steps") or j.get("step"), j["loss"]))
            except: pass
    peak=None
    vram=out/"logs"/f"vram_arm{arm}.log"
    if vram.exists():
        vals=[int(x.strip()) for x in open(vram) if x.strip().isdigit()]
        peak=max(vals) if vals else None
    canary={}
    cj=out/"logs"/f"canary_arm{arm}.json"
    if cj.exists():
        canary=json.load(open(cj))
    report.append({"arm":arm,"final_loss":losses[-1] if losses else None,"n_loss":len(losses),"peak_vram_mb":peak,"canary":canary,"dph":dph})
pathlib.Path("/workspace/deephat-v2/reports/exp1_results_raw.json").write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
PY
log "ALL DONE"
