#!/usr/bin/env python3
"""Sanitize Claude Code session transcripts before they leave the machine.

Redacts API keys, tokens, credentials, emails, and known secret env values
from transcript JSONL files so they can be safely analyzed by the reflection
pass (issue #747) or attached to a proposal PR.

Usage:
  python scripts/sanitize_transcript.py <transcript.jsonl> [...] -o <out_dir>

Sanitization MUST run locally, before any upload — transcripts contain
trading logic and API interactions. Redaction is regex-based and therefore
best-effort: treat sanitized transcripts as sensitive anyway and keep
retention short.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Patterns for secrets and PII. Order matters: most specific first.
REDACTION_PATTERNS = [
    # Provider API keys / tokens
    ("ANTHROPIC_KEY", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("OPENAI_STYLE_KEY", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("NVIDIA_NIM_KEY", re.compile(r"nvapi-[A-Za-z0-9_-]{10,}")),
    ("GITHUB_TOKEN", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GITHUB_PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("SLACK_TOKEN", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("BEARER", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
    ("PRIVATE_KEY_BLOCK", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
    # KEY=value style assignments for secret-looking variable names
    ("ENV_SECRET", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD|"
        r"CREDENTIAL|PRIVATE_KEY)[A-Z0-9_]*)\s*[=:]\s*['\"]?"
        r"(?!REDACTED)([^\s'\"]{8,})['\"]?")),
    # PII
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    # Long hex strings (potential secrets/hashes of credentials)
    ("HEX_SECRET", re.compile(r"\b[0-9a-fA-F]{40,}\b")),
]


def sanitize_text(text, counts):
    for name, pattern in REDACTION_PATTERNS:
        if name == "ENV_SECRET":
            def env_repl(m):
                counts[name] = counts.get(name, 0) + 1
                return f"{m.group(1)}=[REDACTED:{name}]"
            text, n = pattern.subn(env_repl, text)
        else:
            text, n = pattern.subn(f"[REDACTED:{name}]", text)
            if n:
                counts[name] = counts.get(name, 0) + n
    return text


def sanitize_value(value, counts):
    if isinstance(value, str):
        return sanitize_text(value, counts)
    if isinstance(value, dict):
        return {k: sanitize_value(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_value(v, counts) for v in value]
    return value


def sanitize_file(path, out_dir):
    counts = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / path.name
    n_lines = 0
    with open(path, encoding="utf-8") as fin, \
            open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                record = sanitize_value(record, counts)
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
            except json.JSONDecodeError:
                # Malformed line: sanitize as raw text rather than dropping it
                fout.write(sanitize_text(line, counts) + "\n")
            n_lines += 1
    return out_path, n_lines, counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="+", help="Transcript JSONL files")
    parser.add_argument("-o", "--out-dir", required=True,
                        help="Directory for sanitized copies")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = {}
    for name in args.transcripts:
        path = Path(name)
        if not path.is_file():
            print(f"[sanitize] SKIP (not a file): {path}", file=sys.stderr)
            continue
        out_path, n_lines, counts = sanitize_file(path, out_dir)
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "clean"
        print(f"[sanitize] {path.name}: {n_lines} lines -> {out_path} ({summary})")
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v

    if total:
        print("[sanitize] Total redactions: "
              + ", ".join(f"{k}={v}" for k, v in sorted(total.items())))
    else:
        print("[sanitize] No redactions needed.")
    # Restrict permissions on output — still sensitive material
    for f in out_dir.iterdir():
        os.chmod(f, 0o600)


if __name__ == "__main__":
    main()
