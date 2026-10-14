#!/usr/bin/env python3
"""
DeepHat V2 refusal / safety-alignment filter.

PRODUCT CONSTRAINT: keep offensive-security capability intact. We therefore
STRIP training pairs/rows that teach the model to REFUSE security/hacking/
exploit tasks (or benign tasks), so the finetune does not re-learn refusals.

Two surfaces:
  * SFT rows (Tulu-3 messages[], OpenHermes conversations[], Trendyol/Fenrir
    system/user/assistant, Magpie instruction/response): detect an assistant
    turn that REFUSES and drop (or downweight) that row.
  * DPO pairs (HelpSteer3, UltraFeedback): if the CHOSEN response is a refusal
    of a security-flavored prompt, drop the pair (it would teach refusal as the
    preferred behavior). CyberNative security-DPO is kept as-is.

Usage:
  python refusal_filter.py --sample tulu3 --in samples/tulu3.json
  python refusal_filter.py --scale  --dataset allenai/tulu-3-sft-mixture ...
"""
import re, json, argparse, sys

# ---------------------------------------------------------------- detectors
REFUSAL_PATTERNS = [
    r"\bI can'?t help (with|you with) that\b",
    r"\bI can'?t (assist|help) with (that|this)\b",
    r"\bI cannot (assist|help|comply|provide|create|do that)\b",
    r"\bI'?m (not able|unable) to (help|assist|provide|create|comply)\b",
    r"\bI'?m sorry,? but I (can'?t|cannot|won'?t)\b",
    r"\bI'?m really sorry,? but I can'?t\b",
    r"\b(against|violates?) my (guidelines|programming|policy|policies)\b",
    r"\bI (can'?t|cannot|won'?t) (provide|create|generate|write|assist with)\b",
    r"\bI (must|have to) (decline|refuse)\b",
    r"\bI won'?t (be able to )?(help|assist|provide)\b",
    r"\bI do(?:es)? not (feel comfortable|condone)\b",
    r"\b(it|this) (would be|is) (illegal|unethical|not appropriate|inappropriate)\b.{0,80}\b(can'?t|cannot|won'?t|unable)\b",
    r"\bas an AI\b.{0,60}\b(can'?t|cannot|unable|not able)\b",
    r"\bI'?m not comfortable\b",
    r"\bI cannot and will not\b",
    r"\bI must emphasize\b.{0,120}\b(illegal|cannot|won'?t provide)\b",
    r"\bprovide .{0,40}for (educational|informational) purposes only\b.{0,120}\b(can'?t|cannot|won'?t)\b",
    r"\bseek (professional|legal) (help|advice)\b.{0,80}\b(can'?t|cannot|unable)\b",
]
REFUSAL_RE = re.compile("|".join(f"(?:{p})" for p in REFUSAL_PATTERNS), re.IGNORECASE)

# Security / offensive-security topical signal
SECURITY_TERMS = [
    "exploit","payload","shellcode","reverse shell","privilege escalation","privesc",
    "buffer overflow","sql injection","sqli","xss","csrf","rce","remote code execution",
    "malware","ransomware","keylogger","rootkit","backdoor","trojan","botnet","c2","command and control",
    "metasploit","msfvenom","cobalt strike","mimikatz","bloodhound","burp","nmap","nessus","sqlmap",
    "penetration test","pentest","red team","vulnerability","cve-","0day","zero-day","zero day",
    "brute force","password crack","hashcat","john the ripper","phishing","social engineering",
    "lateral movement","persistence","evasion","obfuscat","bypass","decrypt","hijack","spoof",
    "port scan","recon","enumeration","fuzzing","firewall","waf","ids","ips","siem",
    "packet","wireshark","tcpdump","kerberoast","ntlm","lsass","dll injection","process injection",
    "disassembl","reverse engineer","deauth","wpa","wep","injection","overflow","heap spray",
    "gadget","rop chain","format string","race condition","token","credential","beacon",
]
SEC_RE = re.compile("|".join(re.escape(t) for t in SECURITY_TERMS), re.IGNORECASE)

def is_refusal(text):
    if not text: return False
    head = text[:1200]  # refusals come early
    return bool(REFUSAL_RE.search(head))

def is_security(text):
    if not text: return False
    return bool(SEC_RE.search(text))

# ---------------------------------------------------------------- row adapters
def sft_turns(row):
    """Return list of (user_text, assistant_text) pairs from a variety of schemas."""
    out=[]
    if "messages" in row and isinstance(row["messages"],list):  # tulu-3
        last_user=""
        for m in row["messages"]:
            r=m.get("role"); c=m.get("content","")
            if r=="user": last_user=c
            elif r=="assistant": out.append((last_user,c))
    elif "conversations" in row and isinstance(row["conversations"],list):  # openhermes/magpie
        last_user=""
        for m in row["conversations"]:
            frm=m.get("from"); val=m.get("value","")
            if frm in ("human","user"): last_user=val
            elif frm in ("gpt","assistant"): out.append((last_user,val))
    elif "assistant" in row:  # trendyol / fenrir
        out.append((row.get("user",""), row.get("assistant","")))
    elif "response" in row and "instruction" in row:  # magpie flat
        out.append((row.get("instruction",""), row.get("response","")))
    return out

def dpo_pair(row, kind):
    """Return (prompt, chosen, rejected) for DPO schemas."""
    if kind=="helpsteer3":
        ctx=row.get("context",[])
        prompt=" ".join(m.get("content","") for m in ctx if isinstance(m,dict)) if isinstance(ctx,list) else str(ctx)
        pref=row.get("overall_preference",0)  # <0 => response1 better, >0 => response2 better
        r1,r2=row.get("response1",""),row.get("response2","")
        if pref<0: chosen,rej=r1,r2
        elif pref>0: chosen,rej=r2,r1
        else: chosen,rej=r1,r2  # tie: treat r1 as chosen
        return prompt,chosen,rej
    if kind=="ultrafeedback":
        prompt=row.get("instruction","")
        comps=row.get("completions",[]) or []
        if not comps: return prompt,"",""
        best=max(comps,key=lambda c:c.get("overall_score") or -1)
        worst=min(comps,key=lambda c:c.get("overall_score") or 1e9)
        return prompt,best.get("response",""),worst.get("response","")
    if kind=="cybernative":
        return row.get("question",""),row.get("chosen",""),row.get("rejected","")
    return "","",""

# ---------------------------------------------------------------- filters
def filter_sft(rows):
    kept,dropped,ex=[],[],[]
    for row in rows:
        refuse=False
        for user,asst in sft_turns(row):
            if is_refusal(asst):
                # strip refusals of BOTH security and benign requests
                refuse=True
                if len(ex)<8: ex.append({"user":user[:160],"asst":asst[:160],"sec":is_security(user+" "+asst)})
                break
        (dropped if refuse else kept).append(row)
    return kept,dropped,ex

def filter_dpo(rows, kind, keep_asis=False):
    if keep_asis:  # CyberNative: chosen = secure/capable already
        return rows,[],[]
    kept,dropped,ex=[],[],[]
    for row in rows:
        prompt,chosen,rej=dpo_pair(row,kind)
        # drop pair when CHOSEN is a refusal of a security-flavored prompt
        if is_refusal(chosen) and is_security(prompt+" "+rej):
            dropped.append(row)
            if len(ex)<8: ex.append({"prompt":prompt[:160],"chosen":chosen[:160]})
        else:
            kept.append(row)
    return kept,dropped,ex

# ---------------------------------------------------------------- scale runner
def run_at_scale(dataset, config, split, kind, out_path, keep_asis=False):
    """Stream the full dataset with huggingface_hub/datasets and write filtered JSONL."""
    from datasets import load_dataset
    ds=load_dataset(dataset,config,split=split,streaming=True)
    nk=nd=0
    with open(out_path,"w") as f:
        for row in ds:
            if kind=="sft":
                keep,_,_=filter_sft([row]); drop=not keep
            else:
                keep,_,_=filter_dpo([row],kind,keep_asis); drop=not keep
            if (not drop):
                f.write(json.dumps(row)+"\n"); nk+=1
            else: nd+=1
    print(f"{dataset}: kept {nk} dropped {nd}")

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--sample"); ap.add_argument("--in",dest="inp")
    ap.add_argument("--kind",default="sft")  # sft|helpsteer3|ultrafeedback|cybernative
    ap.add_argument("--keep-asis",action="store_true")
    a=ap.parse_args()
    rows=json.load(open(a.inp))
    if a.kind=="sft":
        kept,dropped,ex=filter_sft(rows)
    else:
        kept,dropped,ex=filter_dpo(rows,a.kind,a.keep_asis)
    print(json.dumps({"dataset":a.sample,"kind":a.kind,"in":len(rows),
        "kept":len(kept),"dropped":len(dropped),
        "drop_pct":round(100*len(dropped)/max(1,len(rows)),2),"examples":ex},indent=1))
