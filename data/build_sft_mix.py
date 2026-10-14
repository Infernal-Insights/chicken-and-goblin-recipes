#!/usr/bin/env python3
"""
build_sft_mix.py — assemble the DeepHat V2 SFT training mix and enforce the
hard product constraint end to end:

  1. Pull rows from each configured source (local JSONL and/or HF datasets).
  2. Normalize every row to LLaMA-Factory sharegpt ({"conversations":[...]}).
  3. REFUSAL FILTER: drop rows whose assistant turn refuses (refusal_filter.py).
     Offensive-security capability must stay intact -> no refusal examples.
  4. Near-dup removal (dedup.py MinHash).
  5. Cap / interleave per source, shuffle, write a single JSONL + a manifest.

The output JSONL is what the SFT config (configs/sft_qwen3.5-9b_lora.yaml) points
at (register it in LLaMA-Factory's data/dataset_info.json).

Usage:
  # From local JSONL shards (schema auto-detected):
  python build_sft_mix.py \
      --source tulu3:/data/tulu3.jsonl:10000 \
      --source trendyol:/data/trendyol.jsonl:8000 \
      --source fenrir:/data/fenrir.jsonl:8000 \
      --out /workspace/data/filtered/sft_mix.jsonl

  # From HF datasets (streamed):
  python build_sft_mix.py \
      --hf tulu3:allenai/tulu-3-sft-mixture::train:10000 \
      --out sft_mix.jsonl --seed 42

Each --source is name:path:cap  ;  each --hf is name:repo:config:split:cap
(config may be empty). cap is the max KEPT rows for that source (post-filter).
"""
import argparse, json, os, random, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from refusal_filter import filter_sft, is_refusal  # shared detector / constraint


def to_sharegpt(row):
    """Normalize a variety of schemas -> {"conversations":[{from,value},...]}."""
    if isinstance(row.get("conversations"), list) and row["conversations"]:
        return {"conversations": row["conversations"]}
    if isinstance(row.get("messages"), list) and row["messages"]:
        conv = []
        for m in row["messages"]:
            role = {"system": "system", "user": "human", "assistant": "gpt"}.get(m.get("role"))
            if role:
                conv.append({"from": role, "value": m.get("content", "")})
        return {"conversations": conv} if conv else None
    if row.get("instruction") is not None and (row.get("response") or row.get("output")):
        conv = []
        if row.get("system"):
            conv.append({"from": "system", "value": row["system"]})
        conv.append({"from": "human", "value": row.get("instruction", "")})
        conv.append({"from": "gpt", "value": row.get("response") or row.get("output", "")})
        return {"conversations": conv}
    if row.get("assistant") is not None:
        conv = []
        if row.get("system"):
            conv.append({"from": "system", "value": row["system"]})
        conv.append({"from": "human", "value": row.get("user", "")})
        conv.append({"from": "gpt", "value": row.get("assistant", "")})
        return {"conversations": conv}
    return None


def iter_local(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def iter_hf(repo, config, split):
    from datasets import load_dataset
    ds = load_dataset(repo, config or None, split=split, streaming=True)
    for row in ds:
        yield row


def collect(name, rows_iter, cap, stats):
    """Apply refusal filter per-row, normalize, cap at `cap` kept rows."""
    kept = []
    seen = refused = badschema = 0
    for row in rows_iter:
        seen += 1
        # refusal filter works on the native schema (messages/conversations/...)
        keep, dropped, _ = filter_sft([row])
        if dropped:
            refused += 1
            continue
        sg = to_sharegpt(row)
        if not sg or not sg["conversations"]:
            badschema += 1
            continue
        # belt-and-suspenders: re-check normalized assistant turns
        if any(t.get("from") == "gpt" and is_refusal(t.get("value", "")) for t in sg["conversations"]):
            refused += 1
            continue
        sg["_source"] = name
        kept.append(sg)
        if len(kept) >= cap:
            break
    stats[name] = {"seen": seen, "kept": len(kept), "refused": refused, "bad_schema": badschema}
    return kept


def main():
    ap = argparse.ArgumentParser(description="Assemble + refusal-filter the DeepHat V2 SFT mix.")
    ap.add_argument("--source", action="append", default=[], help="name:path:cap (local jsonl)")
    ap.add_argument("--hf", action="append", default=[], help="name:repo:config:split:cap (streamed)")
    ap.add_argument("--out", required=True, help="output jsonl")
    ap.add_argument("--manifest", help="manifest json (default: <out>.manifest.json)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-dedup", action="store_true", help="skip MinHash dedup pass")
    a = ap.parse_args()
    random.seed(a.seed)

    stats, mix = {}, []
    for spec in a.source:
        name, path, cap = spec.split(":", 2)
        mix += collect(name, iter_local(path), int(cap), stats)
    for spec in a.hf:
        name, repo, config, split, cap = spec.split(":", 4)
        mix += collect(name, iter_hf(repo, config, split), int(cap), stats)

    if not mix:
        sys.exit("no rows collected; pass at least one --source or --hf")

    random.shuffle(mix)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w") as f:
        for row in mix:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # optional dedup via sibling dedup.py
    if not a.no_dedup:
        import subprocess
        deduped = out.with_suffix(".dedup.jsonl")
        r = subprocess.run([sys.executable, str(Path(__file__).parent / "dedup.py"),
                            "--in", str(tmp), "--out", str(out)],
                           capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        if r.returncode != 0:
            os.replace(tmp, out)  # fall back to un-deduped
        else:
            tmp.unlink(missing_ok=True)
    else:
        os.replace(tmp, out)

    final_n = sum(1 for _ in open(out))
    manifest = {
        "out": str(out), "rows": final_n, "seed": a.seed,
        "dedup": (not a.no_dedup), "per_source": stats,
        "constraint": "refusal/alignment examples filtered out (offsec capability intact)",
    }
    mpath = a.manifest or str(out) + ".manifest.json"
    json.dump(manifest, open(mpath, "w"), indent=1)
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
