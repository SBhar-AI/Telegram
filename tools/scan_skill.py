#!/usr/bin/env python3
"""Static risk scan for a third-party Claude Skill / repo / prompt pack.

Read-only: never imports, executes or installs anything it scans.
Usage: python3 scan_skill.py <path-to-dir-or-file> [--json]
Maps findings to the risk indicators in Anthropic's "Skills for enterprise" guide, plus
supply-chain extras (obfuscation, install hooks, persistence, hidden Unicode).
"""
import hashlib
import json
import os
import re
import sys
import unicodedata

SCRIPT_EXT = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".bat", ".cmd", ".rb", ".pl", ".php", ".go", ".rs"}
BINARY_EXT = {".exe", ".dll", ".so", ".dylib", ".bin", ".jar", ".class", ".pyc", ".whl", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".dmg", ".pkg", ".msi", ".apk", ".deb", ".rpm", ".appimage"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
MAX_BYTES = 2_000_000

# (category, concern, regex, description)
RULES = [
    # Instruction manipulation (High)
    ("instruction_manipulation", "HIGH", r"(?i)\b(ignore|disregard|bypass|forget|override)\s+(all\s+|any\s+|your\s+|the\s+)?(previous|prior|above|earlier|system|safety|security|anthropic'?s?|claude'?s?)\b.{0,25}\b(instructions?|rules?|guidelines?|guardrails?|polic(y|ies)|prompts?|checks?)\b", "Directive to ignore/override instructions or safety rules"),
    ("instruction_manipulation", "HIGH", r"(?i)\b(do not|don't|never|without)\s+(tell|inform|notify|alert|let)(ing)?\s+(the\s+)?(user|human|operator)\b|\b(hide|conceal|mask)\b.{0,40}\bfrom (the )?(user|human|operator)\b", "Directive to hide actions from the user"),
    ("instruction_manipulation", "HIGH", r"(?i)\b(silently|secretly|covertly|quietly)\s+(send|upload|post|read|copy|collect|run|execute|install|download|access|transmit|forward|delete|exfiltrat\w*)\b|\bwithout (the )?user'?s? (knowledge|noticing|consent|awareness)\b", "Covert-action language"),
    ("instruction_manipulation", "HIGH", r"(?i)\b(jailbreak|DAN mode|unfiltered mode|god mode)\b|\byou (now )?have no (restrictions|limits|rules)\b|\bwithout any (restrictions|safety)\b", "Jailbreak phrasing"),
    ("instruction_manipulation", "MEDIUM", r"(?i)\bif (the )?(user|input|message|request)\b.{0,60}\b(contains|mentions|says|includes|is from)\b.{0,80}\b(then|instead|switch|always)\b", "Conditional behavior change based on input (review intent)"),
    ("instruction_manipulation", "HIGH", r"(?i)\b(you are now|from now on you|new instructions?:|system prompt:)\b", "Role/instruction injection marker"),
    # Data exfiltration (High)
    ("exfiltration", "HIGH", r"(?i)\b(send|post|upload|transmit|forward|exfiltrat\w*|leak|beacon)\b.{0,60}\b(token|secret|key|password|credential|cookie|env|\.env|ssh|history|conversation|file contents?|data)\b", "Instruction to send sensitive data somewhere"),
    ("exfiltration", "HIGH", r"(?i)!\[[^\]]*\]\(https?://[^)]*(\{|%7B|\$\{|\?[^)]*=)", "Markdown image with templated/query URL (response-channel exfiltration)"),
    ("exfiltration", "HIGH", r"(?i)\b(encode|base64|hex|rot13|compress)\b.{0,50}\b(and|then)\b.{0,30}\b(send|include|append|embed|post|output)\b", "Encode-then-send pattern"),
    # Network access (High)
    ("network", "HIGH", r"(?i)\b(curl|wget|Invoke-WebRequest|iwr|nc|ncat|netcat|socat|telnet|scp|rsync|ftp)\b\s", "Network CLI command"),
    ("network", "HIGH", r"(?i)\b(requests\.(get|post|put|patch|delete|request)|urllib\.request|urlopen|http\.client|httpx\.|aiohttp|socket\.socket|fetch\(|axios|XMLHttpRequest|WebSocket\(|net\.connect|https?\.request)\b", "Network call in code"),
    ("network", "MEDIUM", r"(?i)\bhttps?://[^\s)\"'>]+", "External URL (verify destination)"),
    ("network", "HIGH", r"(?i)\b(webhook\.site|requestbin|pipedream|ngrok|serveo|localtunnel|interact\.sh|burpcollaborator|oast\.|pastebin\.com|transfer\.sh|discord(app)?\.com/api/webhooks|api\.telegram\.org/bot)\b", "Known exfiltration/tunnel endpoint"),
    ("network", "HIGH", r"(?i)\bhttps?://\d{1,3}(\.\d{1,3}){3}", "Raw-IP URL"),
    # Code execution (High)
    ("code_execution", "HIGH", r"(?i)\b(eval|exec)\s*\(|\bnew Function\(|\bcompile\(.{0,40}exec|__import__\(|importlib\.import_module\(", "Dynamic code evaluation"),
    ("code_execution", "HIGH", r"(?i)\b(subprocess\.(run|call|Popen|check_output)|os\.(system|popen|exec\w*)|child_process|execSync|spawn\(|shell=True|Runtime\.getRuntime)\b", "Process spawning"),
    ("code_execution", "HIGH", r"(?i)(curl|wget)[^\n|]{0,200}\|\s*(sudo\s+)?(ba|z)?sh\b|\|\s*python3?\s*-?\s*$|iex\s*\(|Invoke-Expression", "Download-and-execute (pipe to shell)"),
    ("code_execution", "HIGH", r"(?i)\b(pip|pip3|npm|pnpm|yarn|gem|cargo|go)\s+(install|i|add|get)\b(?!.*--dry-run)", "Package install instruction (supply-chain surface)"),
    # Install hooks / persistence (High)
    ("install_hooks", "HIGH", r"(?i)\"(pre|post)?install\"\s*:|\bcmdclass\s*=|setup\.py\s+install|\bpostinstall\b", "Install-time hook"),
    ("persistence", "HIGH", r"(?i)\b(crontab|launchctl|systemctl\s+enable|LaunchAgents|\.bashrc|\.zshrc|\.profile|authorized_keys|HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run|schtasks)\b", "Persistence mechanism"),
    ("privilege", "HIGH", r"(?i)\b(sudo|chmod\s+(\+s|[0-7]*7[0-7]{2})|chown\s+root|setuid)\b", "Privilege escalation / permission change"),
    # Credentials (High)
    ("hardcoded_credentials", "HIGH", r"\b(sk-ant-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{32,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_\-]{35}|\d{8,10}:[A-Za-z0-9_-]{35})\b", "Token-shaped secret"),
    ("hardcoded_credentials", "HIGH", r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "Private key"),
    ("hardcoded_credentials", "MEDIUM", r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer)\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]", "Credential-looking assignment"),
    # Sensitive data access (High when combined with network)
    ("sensitive_read", "HIGH", r"(?i)(~|\$HOME|%USERPROFILE%)?/?\.(ssh|aws|gnupg|kube|docker|npmrc|pypirc|netrc|git-credentials)\b|id_rsa|\.env\b|keychain|Login Data|Cookies\b|wallet\.dat|/etc/(passwd|shadow)", "Access to credential/secret stores"),
    # MCP references (High)
    ("mcp_reference", "HIGH", r"\b[A-Za-z][\w-]*:[a-z][a-z0-9_]{2,}\b(?=[^\n]{0,80}\b(tool|call|use|invoke|run)\b)|mcp__[\w-]+__\w+", "MCP tool reference (ServerName:tool_name)"),
    # Filesystem scope (Medium)
    ("filesystem_scope", "MEDIUM", r"\.\./|(?<![\w.])/(etc|var|usr|root|home|Users|private|System|Library|opt)/|[A-Z]:\\\\|\*\*/\*|\brm\s+-rf\b|shutil\.rmtree|os\.remove|unlink\(", "Path outside skill dir, broad glob, or deletion"),
    # Tool calls (Medium)
    ("tool_calls", "MEDIUM", r"(?i)\b(run|execute|use)\b.{0,20}\b(bash|shell|terminal|the (Bash|Write|Edit) tool|computer use|browser)\b", "Instructs Claude to use tools"),
    # Obfuscation (High)
    ("obfuscation", "HIGH", r"[A-Za-z0-9+/]{200,}={0,2}", "Long base64-like blob"),
    ("obfuscation", "HIGH", r"(\\x[0-9a-fA-F]{2}){20,}|(\\u[0-9a-fA-F]{4}){20,}|(%[0-9a-fA-F]{2}){30,}", "Long escaped/encoded sequence"),
    ("obfuscation", "MEDIUM", r"(?i)\b(b64decode|atob|fromCharCode|codecs\.decode|bytes\.fromhex|zlib\.decompress|marshal\.loads|pickle\.loads)\b", "Decode/deserialize primitive"),
    ("obfuscation", "HIGH", r"(?i)\b(exec|eval|Function)\s*\(.{0,80}\b(b64decode|atob|fromCharCode|decompress|fromhex|unhexlify|marshal\.loads)", "Execute-decoded-payload"),
]
COMPILED = [(c, lvl, re.compile(rx), d) for c, lvl, rx, d in RULES]
NEGATION = re.compile(r"(?i)\b(never|not|no|nothing|n't|avoid|prevent|block|refuse|reject|detect|flag|warn)\b")
NEGATABLE = {"exfiltration", "instruction_manipulation"}

HIDDEN_CHARS = {chr(c) for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x00AD)}


def iter_files(root):
    if os.path.isfile(root):
        yield root
        return
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            yield os.path.join(d, f)


def scan(root):
    findings, inventory, digests = [], {"scripts": [], "binaries": [], "markdown": [], "other": [], "symlinks": []}, {}
    base = root if os.path.isdir(root) else os.path.dirname(root) or "."
    for path in iter_files(root):
        rel = os.path.relpath(path, base)
        ext = os.path.splitext(path)[1].lower()
        if os.path.islink(path):
            inventory["symlinks"].append(f"{rel} -> {os.readlink(path)}")
            findings.append(("filesystem_scope", "HIGH", rel, 0, "Symlink (may point outside the skill)", os.readlink(path)))
            continue
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_BYTES)
        digests[rel] = hashlib.sha256(raw).hexdigest()
        is_binary = b"\x00" in raw[:8192]
        if ext in BINARY_EXT or is_binary:
            inventory["binaries"].append(rel)
            findings.append(("code_execution", "HIGH", rel, 0, "Binary/archive file - cannot be reviewed as text; hash-check (e.g. VirusTotal) before trusting", f"{size} bytes"))
            continue
        bucket = "scripts" if ext in SCRIPT_EXT or raw.startswith(b"#!") else ("markdown" if ext in {".md", ".mdx", ".txt"} else "other")
        inventory[bucket].append(rel)
        if bucket == "scripts":
            findings.append(("code_execution", "HIGH", rel, 0, "Script file present (runs with full environment access if executed)", ""))
        text = raw.decode("utf-8", "replace")
        body = text[1:] if text.startswith(chr(0xFEFF)) else text
        hidden = {c for c in body if c in HIDDEN_CHARS or unicodedata.category(c) == "Cf"}
        if hidden:
            names = ", ".join(sorted(unicodedata.name(c, hex(ord(c))) for c in hidden))
            findings.append(("obfuscation", "HIGH", rel, 0, "Hidden/bidirectional Unicode characters (can conceal instructions)", names))
        if bucket == "scripts":
            longest = max((len(l) for l in text.splitlines()), default=0)
            if longest > 1000:
                findings.append(("obfuscation", "HIGH", rel, 0, "Minified/very long line in script", f"{longest} chars"))
        for lineno, line in enumerate(text.splitlines(), 1):
            for cat, lvl, rx, desc in COMPILED:
                m = rx.search(line)
                if m:
                    if (cat in NEGATABLE and lvl == "HIGH" and "Directive to hide" not in desc
                            and NEGATION.search(line[max(0, m.start() - 40):m.start()])):
                        lvl, desc = "MEDIUM", desc + " (negated/defensive mention; confirm)"
                    findings.append((cat, lvl, rel, lineno, desc, line.strip()[:160]))
    cats = {f[0] for f in findings}
    combos = []
    if "network" in cats and "sensitive_read" in cats:
        combos.append("Reads secret/credential stores AND has network access -> exfiltration risk")
    high_obf = any(f[0] == "obfuscation" and f[1] == "HIGH" for f in findings)
    if high_obf and ("network" in cats or "code_execution" in cats):
        combos.append("Obfuscation combined with network/code execution")
    review_combos = []
    if "network" in cats and ("code_execution" in cats or "filesystem_scope" in cats):
        review_combos.append("File/code access combined with network access (assess combined risk, checklist step 6)")
    urls = sorted({m.group(0).rstrip(".,;:") for f in findings if f[0] == "network"
                   for m in [re.search(r"https?://[^\s)\"'>]+", f[5] or "")] if m})
    domains = sorted({re.sub(r"^https?://([^/:]+).*", r"\1", u).lower() for u in urls})
    return {"root": root, "inventory": inventory, "findings": findings, "combined_risks": combos,
            "review_combos": review_combos,
            "domains": domains, "sha256": digests}


def auto_tier(res):
    levels = [f[1] for f in res["findings"]]
    cats = {f[0] for f in res["findings"] if f[1] == "HIGH"}
    if res["combined_risks"] or cats & {"exfiltration", "hardcoded_credentials", "persistence", "obfuscation"}:
        return "BLOCK-candidate"
    if "HIGH" in levels or res.get("review_combos"):
        return "HIGH-review"
    if "MEDIUM" in levels:
        return "MEDIUM-review"
    return "LOW"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    res = scan(sys.argv[1])
    res["auto_tier"] = auto_tier(res)
    if "--json" in sys.argv:
        print(json.dumps(res, indent=1))
        return
    inv = res["inventory"]
    print(f"AUTO TIER: {res['auto_tier']}   (static signals only - Claude must still do the manual checklist)")
    print("Inventory: " + ", ".join(f"{k}={len(v)}" for k, v in inv.items()))
    for k in ("scripts", "binaries", "symlinks"):
        if inv[k]:
            print(f"  {k}: " + ", ".join(inv[k][:30]))
    if res["domains"]:
        print("External domains: " + ", ".join(res["domains"]))
    for c in res["combined_risks"]:
        print(f"!! COMBINED (block-level): {c}")
    for c in res.get("review_combos", []):
        print(f"!  COMBINED (review): {c}")
    seen = set()
    for cat, lvl, rel, ln, desc, snip in sorted(res["findings"], key=lambda f: (f[1] != "HIGH", f[0], f[2], f[3])):
        key = (cat, rel, ln, desc)
        if key in seen:
            continue
        seen.add(key)
        loc = f"{rel}:{ln}" if ln else rel
        print(f"[{lvl}] {cat:24} {loc:40} {desc}" + (f"\n        > {snip}" if snip else ""))
    print("\nSHA-256 (record these for the registry / integrity check):")
    for rel, h in sorted(res["sha256"].items()):
        print(f"  {h}  {rel}")


if __name__ == "__main__":
    main()
