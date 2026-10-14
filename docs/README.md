# docs/ — pointer only

The **canonical** DeepHat V2 ("Chicken and Goblin recipes") roadmap and team
sync live on the **HuggingFace private repo**, not here. This GitHub repo holds
pipeline **code** only; narrative/planning/weights/data/evals live on HF.

Canonical docs (single source of truth, no forks):

- **Roadmap** — `docs/roadmap.md` on
  `Infernal-Insights/chicken-and-goblin-recipes` (HF private). Covers the
  knowledge roadmap (§A) and the staged release plan (§B), merged into one
  document.
- **TEAM_SYNC** — `docs/TEAM_SYNC.md` on the same HF repo.

Method roadmap (summary): **CPT → SFT → DPO → GRPO/RLVR**. Specialized LoRA
adapters (RE / AD / cloud) during development, merged at v1.0.

Hard constraint (applies everywhere): offensive-security behavior stays fully
intact — no safety/refusal softening. See the top-level `README.md`.

> Do not duplicate the roadmap text here; update it on HF so there is one
> source of truth.
