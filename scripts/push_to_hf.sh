#!/bin/bash
# push_to_hf.sh — push adapters / merged weights / data / eval artifacts to the
# PRIVATE HuggingFace repo (weights+data+evals live on HF; this GitHub repo is
# CODE only). Reads HF_WRITE_TOKEN from env. NEVER prints the token.
#
# Requires: pip install -U "huggingface_hub[cli]"
#
# Usage:
#   HF_WRITE_TOKEN=hf_... ./scripts/push_to_hf.sh adapter ./out/lora_adapter sft-v0.1
#   HF_WRITE_TOKEN=hf_... ./scripts/push_to_hf.sh model   ./out/merged        main
#   HF_WRITE_TOKEN=hf_... ./scripts/push_to_hf.sh folder  ./data/filtered     main  data/filtered
set -uo pipefail

: "${HF_WRITE_TOKEN:?set HF_WRITE_TOKEN (write-scoped HF token) in env; never commit it}"
REPO="${HF_REPO:-Infernal-Insights/chicken-and-goblin-recipes}"
REPO_TYPE="${HF_REPO_TYPE:-model}"   # model | dataset

KIND="${1:?usage: push_to_hf.sh <adapter|model|folder|readme> <path> [revision] [path_in_repo]}"
SRC="${2:?missing source path}"
REV="${3:-main}"
INREPO="${4:-}"

export HF_TOKEN="$HF_WRITE_TOKEN"   # huggingface_hub reads this; not echoed

# Ensure private repo exists, create branch if needed. All via python so the
# token never lands on a command line / in shell history.
python3 - "$REPO" "$REPO_TYPE" "$REV" <<'PY'
import os, sys
from huggingface_hub import HfApi
repo, rtype, rev = sys.argv[1], sys.argv[2], sys.argv[3]
api = HfApi(token=os.environ["HF_TOKEN"])
api.create_repo(repo, repo_type=rtype, private=True, exist_ok=True)
if rev != "main":
    try: api.create_branch(repo, branch=rev, repo_type=rtype, exist_ok=True)
    except Exception as e: print("branch note:", e)
print(f"[hf] repo ready: {repo} (private, revision={rev})")
PY

case "$KIND" in
  readme)
    hf upload "$REPO" "$SRC" README.md --repo-type "$REPO_TYPE" --revision "$REV" \
      --commit-message "Update card";;
  folder)
    DEST="${INREPO:-$(basename "$SRC")}"
    hf upload "$REPO" "$SRC" "$DEST" --repo-type "$REPO_TYPE" --revision "$REV" \
      --commit-message "Push folder -> $DEST@$REV";;
  adapter)
    hf upload "$REPO" "$SRC" "${INREPO:-adapter}" --repo-type "$REPO_TYPE" --revision "$REV" \
      --commit-message "Push adapter -> $REV";;
  model)
    hf upload "$REPO" "$SRC" "${INREPO:-.}" --repo-type "$REPO_TYPE" --revision "$REV" \
      --commit-message "Push merged model -> $REV";;
  *) echo "unknown kind: $KIND"; exit 2;;
esac
echo "[hf] done -> https://huggingface.co/$REPO (private, revision=$REV)"
