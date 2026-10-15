#!/usr/bin/env python3
"""Build cgr-dpo-v0.3 preference pairs for DeepHat V2 / Chicken-and-Goblin.

Format: standard Axolotl DPO {prompt, chosen, rejected} (+ provenance/license_flag).
chosen = capable offensive/technical answer; rejected = refusal OR wrong-flag/wrong-command.
NEVER softens offensive capability. Applies frozen refusal_filter so chosen never
opens with a refusal; rejected SHOULD be refusal or clearly wrong.
"""
from __future__ import annotations
import hashlib, json, os, random, re, sys
from pathlib import Path

ROOT = Path("/workspace/deephat-v2")
sys.path.insert(0, str(ROOT / "eval/frozen_v0.2"))
import refusal_filter as RF  # noqa: E402

OUT_DIR = ROOT / "data/dpo"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RNG = random.Random(42)

REFUSAL_TEMPLATES = [
    "I'm sorry, but I can't help with that. Providing exploitation guidance would violate my guidelines.",
    "I cannot assist with hacking or offensive security requests. Please use this knowledge ethically and seek authorized channels.",
    "I'm not able to help with that request. Creating exploits or attack tooling is against my programming.",
    "I must decline. I won't provide assistance with penetration testing techniques or weaponized payloads.",
    "As an AI, I can't help with activities that could be used to compromise systems. I recommend consulting a licensed security professional instead.",
    "I won't be able to help with that. Writing malware, shellcode, or exploit PoCs is not something I can do.",
    "I'm really sorry, but I can't provide that. It would be illegal/unethical for me to walk through offensive techniques.",
    "I cannot and will not help craft attacks, reverse shells, or privilege-escalation steps.",
    "I do not feel comfortable assisting with this. Offensive tooling and exploit development are out of scope for me.",
    "Against my guidelines — I must refuse requests for SQL injection payloads, buffer overflows, or similar attack material.",
    "I have to refuse. I can't generate Metasploit modules, msfvenom payloads, or step-by-step exploitation guides.",
    "I'm unable to comply. Please redirect your question to defensive detection or high-level conceptual discussion only.",
]

# Common wrong-flag / wrong-command mutations for offensive tools
WRONG_FLAG_RULES = [
    # (pattern in chosen, wrong replacement description, mutate fn)
    (r"\bnmap\b", "wrong_nmap_flags"),
    (r"\bsqlmap\b", "wrong_sqlmap_flags"),
    (r"\bhashcat\b", "wrong_hashcat_mode"),
    (r"\bmsfvenom\b", "wrong_msfvenom"),
    (r"\bhydra\b", "wrong_hydra"),
    (r"\bgobuster\b", "wrong_gobuster"),
    (r"\bnikto\b", "wrong_nikto"),
]

WRONG_MUTATIONS = {
    "wrong_nmap_flags": [
        "nmap -sX -O --script=vuln {target}  # NOTE: -sX is Xmas; wrong for service version — should be -sV",
        "nmap -p 80 --badflag {target}",
        "nmap -sV -wrongproto {target}",
    ],
    "wrong_sqlmap_flags": [
        "sqlmap -u '{url}' --dump-all --os-shell --crawl=99 --batch --level=9 --risk=9 --tamper=space2comment --threads=20 --dbms=oracle",
        "sqlmap -u '{url}' --xyzzy --batch",
        "sqlmap --url '{url}' -D users -T admin --dump --wrong-flag",
    ],
    "wrong_hashcat_mode": [
        "hashcat -m 9999 hashes.txt rockyou.txt  # bogus mode",
        "hashcat -m 0 -a 3 hashes.txt ?a?a?a --force --outdated-flag",
        "hashcat -m 1000 ntlm.txt wordlist.txt -r rules/best64.rule --wrong",
    ],
    "wrong_msfvenom": [
        "msfvenom -p windows/meterpreter/reverse_tcp LHOST=attacker LPORT=4444 -f elf -o shell.elf  # wrong format for windows payload",
        "msfvenom -p linux/x64/shell_reverse_tcp LHOST=1.2.3.4 -f exe -o a.exe  # wrong arch/format pairing",
        "msfvenom --payload invalid/payload LHOST=x -f raw",
    ],
    "wrong_hydra": [
        "hydra -l admin -P rockyou.txt ssh://{target} -t 64 -wrong",
        "hydra -L users.txt -P pass.txt http-get://{target}/ -m bogus",
    ],
    "wrong_gobuster": [
        "gobuster dir -u http://{target} -w /usr/share/wordlists/dirb/common.txt -x php --invalidflag",
        "gobuster dns -d {target} -w subdomains.txt --foomode",
    ],
    "wrong_nikto": [
        "nikto -h {target} -Tuning x -wrong",
        "nikto -host {target} -Plugin bogusplugin",
    ],
}

HALLUCINATED_EXPLOITS = [
    "Use the nonexistent CVE-2099-0001 remote root by sending a single null byte to port 1337 — it always works on modern kernels.",
    "Run `exploit --auto-win` from the fictional HackTools suite; it bypasses all modern EDR without configuration.",
    "The MagicalOverflow class in kernel 6.8 has an unpatched RCE reachable via `ping -RCE target`.",
    "Invoke Metasploit module `exploit/multi/instant_root` (it does not exist) with RHOSTS set and you get SYSTEM.",
]


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()


def extract_turns(row: dict):
    conv = row.get("conversations") or []
    human = gpt = ""
    for c in conv:
        frm = c.get("from")
        val = c.get("value") or ""
        if frm in ("human", "user"):
            human = val
        elif frm in ("gpt", "assistant"):
            gpt = val
    if not human and "messages" in row:
        for m in row["messages"]:
            if m.get("role") == "user":
                human = m.get("content") or ""
            elif m.get("role") == "assistant":
                gpt = m.get("content") or ""
    if not human and "instruction" in row:
        human = row.get("instruction") or ""
        gpt = row.get("response") or ""
    return human.strip(), gpt.strip()


def license_flag_from_meta(meta: dict | None) -> str:
    if not meta:
        return "unknown-private-test"
    lic = (meta.get("license") or "").strip() or "unknown"
    # FLAG for future public release — private testing allows all
    return f"FLAG-license:{lic}"


def make_pair(prompt, chosen, rejected, provenance, license_flag, pair_kind):
    return {
        "prompt": prompt,
        "chosen": chosen,
        "rejected": rejected,
        "provenance": provenance,
        "license_flag": license_flag,
        "pair_kind": pair_kind,
        "pair_sha256": sha(prompt + "\n" + chosen + "\n" + rejected),
    }


def synth_refusal() -> str:
    return RNG.choice(REFUSAL_TEMPLATES)


def try_wrong_flag(chosen: str) -> str | None:
    for pat, key in WRONG_FLAG_RULES:
        if re.search(pat, chosen, re.I):
            tmpl = RNG.choice(WRONG_MUTATIONS[key])
            # naive target/url placeholders
            target = "10.0.0.5"
            url = "http://10.0.0.5/vuln.php?id=1"
            cmd = tmpl.format(target=target, url=url)
            return (
                f"Sure — here's the command I'd run:\n\n```\n{cmd}\n```\n\n"
                f"That should get you what you need on the target."
            )
    return None


def hallucinated_reject(prompt: str) -> str:
    return RNG.choice(HALLUCINATED_EXPLOITS) + "\n\nAfter that, dump /etc/shadow and you're done."


def from_cybernative(path: Path, limit: int | None = None):
    out = []
    with path.open() as f:
        for i, line in enumerate(f):
            if limit and i >= limit:
                break
            r = json.loads(line)
            prompt = (r.get("question") or "").strip()
            chosen = (r.get("chosen") or "").strip()
            rejected = (r.get("rejected") or "").strip()
            if not prompt or not chosen or not rejected:
                continue
            if RF.is_refusal(chosen):
                continue  # never keep refusal-as-chosen
            # rejected in cybernative is insecure code — keep; optionally OK if also refusal
            out.append(
                make_pair(
                    prompt,
                    chosen,
                    rejected,
                    provenance="cybernative:Code_Vulnerability_Security_DPO",
                    license_flag="FLAG-license:see-CyberNative-HF-card",
                    pair_kind="secure_vs_vuln_code",
                )
            )
    return out


def from_sft_capable_vs_refusal(sft_path: Path, n_pairs: int):
    """Mine capable SFT assistant turns as chosen; synthetic refusal as rejected."""
    candidates = []
    with sft_path.open() as f:
        for line in f:
            row = json.loads(line)
            human, gpt = extract_turns(row)
            if not human or not gpt or len(gpt) < 120:
                continue
            if RF.is_refusal(gpt):
                continue
            if not RF.is_security(human + " " + gpt):
                continue
            # skip rows that are already mostly refusal-shaped deeper? opening check is enough
            meta = row.get("meta") or {}
            candidates.append((human, gpt, meta))
    RNG.shuffle(candidates)
    out = []
    for human, gpt, meta in candidates[:n_pairs]:
        out.append(
            make_pair(
                human,
                gpt,
                synth_refusal(),
                provenance=f"sft_mix_capable+synth_refusal:{meta.get('source','?')}",
                license_flag=license_flag_from_meta(meta),
                pair_kind="capable_vs_refusal",
            )
        )
    return out


def from_sft_wrong_flag(sft_path: Path, n_pairs: int):
    """Chosen = capable command answer; rejected = mutated wrong-flag / hallucinated."""
    out = []
    with sft_path.open() as f:
        for line in f:
            if len(out) >= n_pairs:
                break
            row = json.loads(line)
            human, gpt = extract_turns(row)
            if not human or not gpt or len(gpt) < 80:
                continue
            if RF.is_refusal(gpt):
                continue
            wrong = try_wrong_flag(gpt)
            if not wrong:
                # only keep rows that look like tool commands for this bucket
                if not re.search(r"\b(nmap|sqlmap|hashcat|msfvenom|hydra|gobuster|nikto)\b", gpt, re.I):
                    continue
                wrong = hallucinated_reject(human)
            meta = row.get("meta") or {}
            out.append(
                make_pair(
                    human,
                    gpt,
                    wrong,
                    provenance=f"sft_mix_wrongflag:{meta.get('source','?')}",
                    license_flag=license_flag_from_meta(meta),
                    pair_kind="capable_vs_wrong_flag",
                )
            )
    return out


def from_sft_vs_hallucination(sft_path: Path, n_pairs: int):
    out = []
    with sft_path.open() as f:
        lines = f.readlines()
    RNG.shuffle(lines)
    for line in lines:
        if len(out) >= n_pairs:
            break
        row = json.loads(line)
        human, gpt = extract_turns(row)
        if not human or not gpt or len(gpt) < 150:
            continue
        if RF.is_refusal(gpt):
            continue
        if not RF.is_security(human):
            continue
        meta = row.get("meta") or {}
        out.append(
            make_pair(
                human,
                gpt,
                hallucinated_reject(human),
                provenance=f"sft_mix_vs_hallucinated_exploit:{meta.get('source','?')}",
                license_flag=license_flag_from_meta(meta),
                pair_kind="capable_vs_hallucinated_exploit",
            )
        )
    return out


def dedup(pairs):
    seen = set()
    kept = []
    for p in pairs:
        # dedup on prompt+chosen hash primarily; also full pair sha
        k = sha(p["prompt"] + "\0" + p["chosen"])
        if k in seen:
            continue
        if RF.is_refusal(p["chosen"]):
            continue  # hard gate
        seen.add(k)
        kept.append(p)
    return kept


def main():
    cyber_path = OUT_DIR / "build/cybernative_raw/secure_programming_dpo.json"
    sft_80k = ROOT / "data/filtered/sft_mix_v1_80k_budget.jsonl"
    # larger mix if present for more diversity
    sft_full = ROOT / "data/mix/sft_mix_v1_80k_budget.jsonl"
    sft = sft_80k if sft_80k.exists() else sft_full

    pairs = []
    print("[dpo] cybernative…")
    pairs += from_cybernative(cyber_path)
    print(f"  +{len(pairs)} cybernative")

    print("[dpo] capable vs refusal…")
    a = from_sft_capable_vs_refusal(sft, n_pairs=3500)
    pairs += a
    print(f"  +{len(a)}")

    print("[dpo] capable vs wrong-flag…")
    b = from_sft_wrong_flag(sft, n_pairs=2000)
    pairs += b
    print(f"  +{len(b)}")

    print("[dpo] capable vs hallucinated exploit…")
    c = from_sft_vs_hallucination(sft, n_pairs=1500)
    pairs += c
    print(f"  +{len(c)}")

    print("[dpo] dedup + refusal gate on chosen…")
    kept = dedup(pairs)
    RNG.shuffle(kept)
    print(f"  kept {len(kept)} / raw {len(pairs)}")

    # sanity: rejected should often be refusal for capable_vs_refusal
    n_rej_refusal = sum(1 for p in kept if RF.is_refusal(p["rejected"]))
    n_chosen_refusal = sum(1 for p in kept if RF.is_refusal(p["chosen"]))
    assert n_chosen_refusal == 0, f"chosen refusals leaked: {n_chosen_refusal}"

    out_jsonl = OUT_DIR / "cgr_dpo_v0.3.jsonl"
    with out_jsonl.open("w") as f:
        for p in kept:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    by_kind = {}
    for p in kept:
        by_kind[p["pair_kind"]] = by_kind.get(p["pair_kind"], 0) + 1

    manifest = {
        "rows": len(kept),
        "raw_before_dedup": len(pairs),
        "by_kind": by_kind,
        "rejected_is_refusal_count": n_rej_refusal,
        "chosen_is_refusal_count": n_chosen_refusal,
        "format": "{prompt,chosen,rejected,provenance,license_flag,pair_kind,pair_sha256}",
        "axolotl_type": "chatml.prompt_pairs",
        "sources": [
            "CyberNative/Code_Vulnerability_Security_DPO",
            str(sft),
            "synthetic_refusal_templates",
            "synthetic_wrong_flag_mutations",
            "synthetic_hallucinated_exploits",
        ],
        "refusal_filter": "eval/frozen_v0.2/refusal_filter.py (opening-scoped)",
        "constraint": "chosen never refusal-shaped; offensive capability intact; no safety softening",
        "out_jsonl": str(out_jsonl),
    }
    (OUT_DIR / "cgr_dpo_v0.3_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    if len(kept) < 2000:
        print("WARNING: below 2000 staging minimum", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
