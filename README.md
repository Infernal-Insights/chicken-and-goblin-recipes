# chicken-and-goblin-recipes

Training-code scaffold for **DeepHat V2** — an offensive-security specialist LLM.
This GitHub repo holds the **pipeline CODE only**. Model weights, datasets, eval
results, and planning docs live on a **separate HuggingFace private repo** of the
same name.

---

## The HF ⟷ GitHub split

| Lives here (GitHub `Infernal-Insights/chicken-and-goblin-recipes`) | Lives on HF (private, same name) |
|---|---|
| Training/eval **code**: LLaMA-Factory & Axolotl configs, launch/watch scripts, data-prep pipeline, eval harness | **Weights** (LoRA adapters + merged models) |
| Config **templates** and stubs | **Datasets** (raw + refusal-filtered blends) |
| Placeholder eval prompts + scoring code | **Eval results** and canary logs |
| — | **Canonical roadmap + TEAM_SYNC** (`docs/`) |

Rule of thumb: anything large, sensitive, or a source-of-truth narrative → HF.
Code that builds/launches/evaluates → here. No weights, data, or `.env` ever land
in git (see `.gitignore`).

---

## ⚠️ Hard constraint — no refusal/safety softening

DeepHat V2 is an **authorized** offensive-security tool. Its security capability
must stay **fully intact**. This is enforced through the whole pipeline:

- **SFT data**: `data/refusal_filter.py` drops rows whose assistant turn *refuses*
  a task (security or benign), so the finetune never re-learns refusals.
- **DPO data**: pairs whose **chosen** response is a refusal of a security prompt
  are dropped. "Chosen" = the security-capable response. CyberNative security-DPO
  is kept as-is.
- **Eval**: `refusal-rate-near-zero` is the key domain metric. `eval/refusal_probe.py`
  gates on it; `score.py`-style scoring flags a regression if refusal rises or
  offsec capability drops vs the DeepHat-V1-7B baseline.

No safety/alignment examples are added back anywhere.

---

## Model & method

- **Base:** `Qwen/Qwen3.5-9B-Base`, **bf16 LoRA** (NOT QLoRA — the DeltaNet/SSM
  hybrid arch quantizes poorly in 4-bit; nf4 wrecks recall + long context).
  `Qwen3-14B-Base` dense 4-bit QLoRA is **insurance only**.
- **Stack:** LLaMA-Factory (`llamafactory-cli`) primary; Axolotl for DPO
  (toolchain continuity with DeepHat V1). **SDPA** attention fallback when
  flash-attn wheels are unavailable. Gradient checkpointing on.
- **LoRA targets:** `q/k/v/o/gate/up/down` — plus the DeltaNet mixer projection
  names, which must be **enumerated against the real model config** before a full
  run.
- **Method roadmap:** CPT → SFT → DPO → GRPO/RLVR. Specialized LoRA adapters
  (RE / AD / cloud) during dev, merged at **v1.0**.
- **Context:** Qwen3.5 ships native 262k context — no RoPE/yarn surgery. Bulk SFT
  at 8k, then a short tail pass at high ctx (32768) on long security docs.

---

## Repo layout

```
.
├── README.md
├── .gitignore
├── configs/
│   ├── exp1_armA_qwen3.5-9b_lora.yaml   # EXP1 Arm A: 9B bf16 LoRA (real)
│   ├── exp1_armB_qwen3-14b_qlora.yaml   # EXP1 Arm B: 14B 4-bit QLoRA (real)
│   ├── sft_qwen3.5-9b_lora.yaml         # SFT TEMPLATE (fill in dataset+output)
│   └── dpo_axolotl.yaml                 # DPO STUB (Axolotl, preference stage)
├── scripts/
│   ├── exp1_remote.sh                   # on-instance driver for one EXP arm (real)
│   ├── watch_exp1.sh                    # poll arms, pull artifacts, destroy (real)
│   ├── vast_launch.sh                   # Vast API wrapper (placeholder; VAST_API_KEY)
│   └── push_to_hf.sh                    # push artifacts to HF (HF_WRITE_TOKEN)
├── data/
│   ├── refusal_filter.py                # refusal/alignment filter (SFT + DPO)
│   ├── dedup.py                         # MinHash/LSH near-dup removal
│   └── build_sft_mix.py                 # assemble + filter + dedup → jsonl
├── eval/
│   ├── eval_harness.md                  # harness description
│   ├── refusal_probe.py                 # refusal-rate probe (gates on near-zero)
│   └── prompts/                         # PLACEHOLDER prompt sets (22 / 15 / 8)
│       ├── offsec_capability.jsonl
│       ├── refusal_probe.jsonl
│       └── general_if.jsonl
└── docs/
    └── README.md                        # pointer: canonical roadmap+sync on HF
```

> The `eval/prompts/*.jsonl` here are **clearly-labeled placeholders** for wiring
> the harness. The real capability/refusal prompt sets live in the HF private repo.

---

## Infra

- **Training:** Vast.ai — A100 80GB for 9B bf16 LoRA (~$1/hr); RTX 4090 for 14B
  QLoRA. Launch via `scripts/vast_launch.sh`, watch/tear-down via
  `scripts/watch_exp1.sh`. Spend requires explicit human approval.
- **Local eval:** Derek's RTX 5070 Ti 16GB, loading candidates 4-bit (nf4).
- **Artifacts:** pushed to the HF private repo with `scripts/push_to_hf.sh`.

---

## Quickstart

```bash
# 0) clone
git clone https://github.com/Infernal-Insights/chicken-and-goblin-recipes.git
cd chicken-and-goblin-recipes

# 1) build a refusal-filtered + deduped SFT mix (local shards or HF datasets)
python data/build_sft_mix.py \
    --source tulu3:/data/tulu3.jsonl:10000 \
    --source trendyol:/data/trendyol.jsonl:8000 \
    --out /workspace/data/filtered/sft_mix.jsonl

# 2) register the jsonl in LLaMA-Factory's data/dataset_info.json, then edit
#    configs/sft_qwen3.5-9b_lora.yaml (dataset + output_dir)

# 3) launch training on Vast (dry-run by default; --confirm needs approval)
VAST_API_KEY=... ./scripts/vast_launch.sh --arm A --gpu A100

# 4) on the instance, exp1_remote.sh drives deps + train + canary:
#    ./scripts/exp1_remote.sh A sft_qwen3.5-9b_lora.yaml

# 5) local refusal gate on the 5070 Ti (must be near-zero)
python eval/refusal_probe.py --model /path/to/candidate \
    --prompts eval/prompts/refusal_probe.jsonl --load-4bit --out refusal_candidate.json

# 6) push adapter/weights to the HF private repo
HF_WRITE_TOKEN=hf_... ./scripts/push_to_hf.sh adapter ./out/lora_adapter sft-v0.1
```

See `eval/eval_harness.md` for the full eval stack (lighteval canaries +
CyBench/NYU CTF Bench/CTFTiny + the custom offsec/refusal/IF sets) and
`docs/README.md` for where the canonical roadmap lives.
