#!/usr/bin/env python3
"""Merge Scout KEV/CVE freshness snapshot into SFT mix rows.

Validates Scout schema; joins on cve_id. See FRESHNESS_JOIN.md.
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

REQUIRED_FIELDS = [
    "cve_id", "severity", "product", "kev_flag", "published_date",
    "fetch_date", "short_impact", "source_url", "license_flag", "provenance_sha256",
]
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)

DEFAULT_REPO = "Infernal-Insights/cgr-freshness-kev"
DEFAULT_REV = "939db0c3594da5f9e0ef4d1455754e32a999f618"
DEFAULT_TAG = "snapshot-2026-10-15"


def norm_cve(s: str) -> str:
    return s.strip().upper()


def validate_freshness_row(row: dict, idx: int):
    missing = [k for k in REQUIRED_FIELDS if k not in row]
    if missing:
        raise SystemExit(f"freshness schema mismatch at row {idx}: missing {missing}")


def load_freshness(path: Path) -> dict[str, dict]:
    if not path.exists():
        raise SystemExit(
            f"freshness snapshot not found: {path}\n"
            f"Expected Scout file with fields: {REQUIRED_FIELDS}\n"
            f"See FRESHNESS_JOIN.md — refuse to invent rows."
        )
    by_cve = {}
    with path.open() as f:
        for i, line in enumerate(f):
            row = json.loads(line)
            validate_freshness_row(row, i)
            cid = norm_cve(row["cve_id"])
            by_cve[cid] = row
    return by_cve


def extract_cves_from_sft(row: dict) -> list[tuple[str, str]]:
    """Return list of (cve_id, matched_via)."""
    hits = []
    meta = row.get("meta") or {}
    for key in ("cve", "cve_id"):
        v = meta.get(key)
        if isinstance(v, str) and CVE_RE.fullmatch(v.strip()):
            hits.append((norm_cve(v), f"meta.{key}"))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str) and CVE_RE.search(item):
                    hits.append((norm_cve(CVE_RE.search(item).group(0)), f"meta.{key}"))
    codes = meta.get("codes") or ""
    if isinstance(codes, str) and codes.strip():
        for m in CVE_RE.findall(codes):
            hits.append((norm_cve(m), "meta.codes"))
    # body
    texts = []
    for c in row.get("conversations") or []:
        texts.append(c.get("value") or "")
    for m in row.get("messages") or []:
        texts.append(m.get("content") or "")
    for k in ("instruction", "response", "prompt", "chosen"):
        if k in row:
            texts.append(row.get(k) or "")
    body = "\n".join(texts)
    for m in CVE_RE.findall(body):
        hits.append((norm_cve(m), "body_regex"))
    # dedupe preserving order
    seen = set()
    out = []
    for cid, via in hits:
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, via))
    return out


def attach(row: dict, fr: dict, via: str, repo: str, rev: str, tag: str, flatten: bool):
    meta = dict(row.get("meta") or {})
    freshness = {k: fr[k] for k in REQUIRED_FIELDS}
    meta["freshness"] = freshness
    meta["freshness_join"] = {
        "snapshot_repo": repo,
        "snapshot_revision": rev,
        "snapshot_tag": tag,
        "matched_via": via,
        "joined_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    if flatten:
        for k, v in freshness.items():
            meta[f"freshness_{k}"] = v
    new = dict(row)
    new["meta"] = meta
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sft", required=True)
    ap.add_argument("--freshness", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--revision", default=DEFAULT_REV)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--mode", choices=["annotate", "explode"], default="annotate",
                    help="annotate=one row with freshness for first hit (+hits list); explode=one row per CVE")
    ap.add_argument("--limit-out", type=int, default=0, help="stop after N joined output rows (0=all joined)")
    ap.add_argument("--max-sft-scan", type=int, default=0, help="optional cap on SFT rows scanned")
    args = ap.parse_args()

    fres = load_freshness(Path(args.freshness))
    print(f"[freshness] loaded {len(fres)} CVEs from {args.freshness}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_scan = n_hit = n_out = 0
    severity_counts = {}
    with open(args.sft) as fin, out_path.open("w") as fout:
        for line in fin:
            if args.max_sft_scan and n_scan >= args.max_sft_scan:
                break
            n_scan += 1
            row = json.loads(line)
            cves = extract_cves_from_sft(row)
            matched = [(cid, via) for cid, via in cves if cid in fres]
            if not matched:
                continue
            n_hit += 1
            if args.mode == "annotate":
                cid, via = matched[0]
                new = attach(row, fres[cid], via, args.repo, args.revision, args.tag, flatten=False)
                new["meta"]["freshness_hits"] = [
                    {"cve_id": c, "matched_via": v, "in_snapshot": True} for c, v in matched
                ]
                fout.write(json.dumps(new, ensure_ascii=False) + "\n")
                n_out += 1
                severity_counts[fres[cid]["severity"]] = severity_counts.get(fres[cid]["severity"], 0) + 1
            else:
                for cid, via in matched:
                    new = attach(row, fres[cid], via, args.repo, args.revision, args.tag, flatten=False)
                    fout.write(json.dumps(new, ensure_ascii=False) + "\n")
                    n_out += 1
                    severity_counts[fres[cid]["severity"]] = severity_counts.get(fres[cid]["severity"], 0) + 1
                    if args.limit_out and n_out >= args.limit_out:
                        break
            if args.limit_out and n_out >= args.limit_out:
                break

    summary = {
        "sft_scanned": n_scan,
        "sft_with_cve_in_snapshot": n_hit,
        "rows_written": n_out,
        "freshness_size": len(fres),
        "snapshot_repo": args.repo,
        "snapshot_revision": args.revision,
        "snapshot_tag": args.tag,
        "severity_counts_joined": severity_counts,
        "schema_fields": REQUIRED_FIELDS,
        "schema_ok": True,
        "out": str(out_path),
    }
    print(json.dumps(summary, indent=2))
    side = out_path.with_suffix(".summary.json")
    side.write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
