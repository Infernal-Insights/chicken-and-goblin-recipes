#!/usr/bin/env python3
"""
dedup.py — near-duplicate removal for DeepHat V2 SFT/DPO JSONL mixes using
MinHash + LSH (datasketch). Dedups on the normalized TEXT of each row (the
concatenated turn text), so paraphrase-level and exact duplicates both drop.

Why: the SFT blend stitches several datasets (Tulu-3, OpenHermes, Trendyol,
Fenrir, WRN, Primus...). Overlap inflates easy examples and biases the model;
dedup keeps the mix honest before training.

Usage:
  python dedup.py --in blend.jsonl --out blend.dedup.jsonl
  python dedup.py --in blend.jsonl --out blend.dedup.jsonl --threshold 0.85 --num-perm 128
  # multiple inputs are concatenated then deduped globally (first occurrence wins)
  python dedup.py --in a.jsonl b.jsonl --out merged.dedup.jsonl

Requires: pip install datasketch
Falls back to exact-hash dedup if datasketch is unavailable.
"""
import argparse, json, re, sys, hashlib

_WS = re.compile(r"\s+")
_NONALNUM = re.compile(r"[^0-9a-z ]+")


def row_text(row):
    """Extract a single normalized text string from a variety of row schemas."""
    parts = []
    if isinstance(row.get("conversations"), list):
        parts = [m.get("value", "") for m in row["conversations"]]
    elif isinstance(row.get("messages"), list):
        parts = [m.get("content", "") for m in row["messages"]]
    elif "prompt" in row:
        parts = [row.get("prompt", ""), row.get("chosen", ""), row.get("rejected", "")]
    elif "instruction" in row:
        parts = [row.get("instruction", ""), row.get("response", row.get("output", ""))]
    else:
        parts = [str(v) for v in row.values() if isinstance(v, str)]
    text = " ".join(p for p in parts if p)
    text = text.lower()
    text = _NONALNUM.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text


def shingles(text, k=5):
    """Word-level k-shingles for MinHash."""
    toks = text.split()
    if len(toks) < k:
        return {text} if text else set()
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}


def read_rows(paths):
    for p in paths:
        with open(p) as f:
            for ln, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    print(f"[warn] {p}:{ln} bad json, skipped", file=sys.stderr)


def dedup_minhash(rows, threshold, num_perm, k):
    from datasketch import MinHash, MinHashLSH
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    kept, dropped = [], 0
    for i, row in enumerate(rows):
        text = row_text(row)
        if not text:
            kept.append(row)
            continue
        m = MinHash(num_perm=num_perm)
        for sh in shingles(text, k):
            m.update(sh.encode("utf-8"))
        if lsh.query(m):          # a near-duplicate already indexed
            dropped += 1
            continue
        lsh.insert(f"r{i}", m)
        kept.append(row)
    return kept, dropped


def dedup_exact(rows):
    seen, kept, dropped = set(), [], 0
    for row in rows:
        h = hashlib.sha1(row_text(row).encode("utf-8")).hexdigest()
        if h in seen:
            dropped += 1
            continue
        seen.add(h)
        kept.append(row)
    return kept, dropped


def main():
    ap = argparse.ArgumentParser(description="MinHash near-dup removal for JSONL mixes.")
    ap.add_argument("--in", dest="inputs", nargs="+", required=True, help="input jsonl file(s)")
    ap.add_argument("--out", required=True, help="output jsonl")
    ap.add_argument("--threshold", type=float, default=0.85, help="Jaccard similarity threshold")
    ap.add_argument("--num-perm", type=int, default=128, help="MinHash permutations")
    ap.add_argument("--k", type=int, default=5, help="shingle size (words)")
    ap.add_argument("--exact", action="store_true", help="force exact-hash dedup only")
    a = ap.parse_args()

    rows = list(read_rows(a.inputs))
    if a.exact:
        kept, dropped = dedup_exact(rows)
        method = "exact"
    else:
        try:
            kept, dropped = dedup_minhash(rows, a.threshold, a.num_perm, a.k)
            method = f"minhash(thr={a.threshold},perm={a.num_perm},k={a.k})"
        except ImportError:
            print("[warn] datasketch not installed -> exact-hash fallback", file=sys.stderr)
            kept, dropped = dedup_exact(rows)
            method = "exact-fallback"

    with open(a.out, "w") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({
        "method": method, "in": len(rows), "kept": len(kept),
        "dropped": dropped, "drop_pct": round(100 * dropped / max(1, len(rows)), 2),
        "out": a.out,
    }, indent=1))


if __name__ == "__main__":
    main()
