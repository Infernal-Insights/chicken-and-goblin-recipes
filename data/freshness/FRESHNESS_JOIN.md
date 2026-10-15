# Freshness join schema (Scout → SFT mix)

**Owner:** LLM Training Lead (join definition). **Producer:** Offsec Data Scout (`Infernal-Insights/cgr-freshness-kev`).

## Purpose

Attach CVE/KEV freshness metadata onto SFT (and later CPT) rows so training/eval can
filter, upsample, or audit by vulnerability currency without re-fetching NVD/KEV.

Scout owns the snapshot rows. This doc + `merge_freshness.py` own the **join contract**.

## Scout snapshot (pinned)

| Field | Value |
|---|---|
| HF dataset | `Infernal-Insights/cgr-freshness-kev` (private) |
| Tag | `snapshot-2026-10-15` |
| Commit SHA | `939db0c3594da5f9e0ef4d1455754e32a999f618` |
| Local path | `/workspace/cgr-freshness-kev/data/train.jsonl` |
| Rows (this pin) | 2107 (1745 KEV + 301 CRITICAL non-KEV + 61 HIGH non-KEV) |

## Join key

Primary: **`cve_id`** (normalized `CVE-YYYY-NNNN+`, uppercase).

Matching surfaces on an SFT row (first hit wins):

1. `meta.cve` / `meta.cve_id` / `meta.codes` (Exploit-DB style CVE list)
2. Regex `CVE-\d{4}-\d{4,}` over `conversations[*].value` / `messages[*].content` / `instruction`+`response`

Multi-CVE rows: emit one joined copy per distinct CVE (default), or keep a
`freshness_hits[]` array when `--mode annotate` (non-exploding).

## Freshness fields merged onto the row

All copied under `meta.freshness` (and optionally flattened with `freshness_` prefix
when `--flatten`):

| Field | Type | Notes |
|---|---|---|
| `cve_id` | string | Normalized CVE id |
| `severity` | string | CRITICAL/HIGH/MEDIUM/LOW/… |
| `product` | string | Affected product label from Scout |
| `kev_flag` | bool | True if on CISA KEV |
| `published_date` | string | ISO date (YYYY-MM-DD) when known |
| `fetch_date` | string | Scout fetch date |
| `short_impact` | string | Short impact blurb |
| `source_url` | string | NVD/advisory URL |
| `license_flag` | string | e.g. `US-gov-public` — **FLAG for future public release** |
| `provenance_sha256` | string | Scout row content hash |

Plus join bookkeeping:

| Field | Notes |
|---|---|
| `meta.freshness_join.snapshot_repo` | HF id |
| `meta.freshness_join.snapshot_revision` | commit SHA |
| `meta.freshness_join.snapshot_tag` | tag name |
| `meta.freshness_join.matched_via` | `meta.cve` \| `meta.codes` \| `body_regex` |
| `meta.freshness_join.joined_at` | ISO timestamp |

## License

Private testing: capability over license. Retain Scout `license_flag` per joined
row for a **future public release** gate. Do not strip.

## Non-goals

- Scout fetch/refresh logic (Scout owns it)
- Mutating chosen/assistant text
- Refusal/safety softening of any kind

## Usage

```bash
python3 merge_freshness.py \
  --sft /workspace/deephat-v2/data/filtered/sft_mix_v1_80k_budget.jsonl \
  --freshness /workspace/cgr-freshness-kev/data/train.jsonl \
  --revision 939db0c3594da5f9e0ef4d1455754e32a999f618 \
  --tag snapshot-2026-10-15 \
  --mode annotate \
  --limit-out 50 \
  --out /workspace/deephat-v2/reports/v0.3_staging/freshness_join_sample.jsonl
```

`merge_freshness.py` validates Scout schema even if the snapshot path is missing
(exits non-zero with a clear schema error). Dry-merge sample is the staging proof.
