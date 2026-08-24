#!/usr/bin/env python3
"""Cost-guard triage pass for the reflection loop (issue #747).

Sends SANITIZED transcript excerpts to DeepSeek (the cheap model already
wired into CI) to flag candidate inefficiencies. Claude is escalated to only
afterwards, to turn confirmed findings into well-written diffs — see
.claude/skills/reflect/SKILL.md.

Env vars (same convention as scripts/ai_reviewer.py):
  DEEPSEEK_API_KEY      — DeepSeek API key (required)
  DEEPSEEK_BASE_URL     — default https://api.deepseek.com
  DEEPSEEK_MODEL        — default deepseek-chat

Usage:
  python scripts/reflect_triage.py <sanitized.jsonl> [...] -o findings.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

TRIAGE_SYSTEM_PROMPT = """\
You are a transcript efficiency auditor for a multi-agent Claude Code team.
You receive numbered turns from an agent session transcript. Flag ONLY:

1. User corrections/rephrasing that clearer upfront instructions would have avoided
2. Tool calls that returned empty or irrelevant results (poor query formulation)
3. Clarifying questions that better agent/skill context would have prevented
4. Repeated re-establishment of the same context across turns (redundant preamble)
5. Over-explanation or unnecessary verbosity caused by instructions
6. Re-querying data that was already provided (e.g. pre-resolved GraphQL IDs)

Output STRICT JSON, no prose:
{"findings": [{"category": "<one of: correction|poor-query|avoidable-question|redundant-context|verbosity|requery>",
  "turns": [<turn indices>], "quote": "<short verbatim excerpt, <=200 chars>",
  "explanation": "<one sentence>", "confidence": "high|medium|low"}]}

If a behavior looks intentional (e.g. mandated governance comments), do not
flag it, or flag it with confidence "low". Return {"findings": []} if clean.
"""

MAX_CHUNK_CHARS = 60_000  # keep well inside the context window per call


def call_deepseek(base_url, api_key, model, system_prompt, user_content):
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 3000,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.post(url, json=payload, headers=headers, timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def extract_turn_text(record):
    """Best-effort compact text rendering of one transcript record."""
    parts = []
    for key in ("type", "role"):
        if isinstance(record.get(key), str):
            parts.append(record[key])
            break
    msg = record.get("message", record)
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                parts.append(f"[tool_use {block.get('name')}] "
                             f"{json.dumps(block.get('input', {}))[:300]}")
            elif btype == "tool_result":
                raw = json.dumps(block.get("content", ""))[:300]
                parts.append(f"[tool_result] {raw}")
    return " ".join(p for p in parts if p).strip()


def chunk_transcript(path):
    """Yield (start_turn, text) chunks under MAX_CHUNK_CHARS."""
    lines = []
    with open(path, encoding="utf-8") as fin:
        for i, line in enumerate(fin):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = extract_turn_text(record)
            if text:
                lines.append((i, f"[turn {i}] {text[:2000]}"))

    chunk, start, size = [], None, 0
    for idx, rendered in lines:
        if start is None:
            start = idx
        if size + len(rendered) > MAX_CHUNK_CHARS and chunk:
            yield start, "\n".join(chunk)
            chunk, start, size = [], idx, 0
        chunk.append(rendered)
        size += len(rendered)
    if chunk:
        yield start, "\n".join(chunk)


def parse_findings(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text).get("findings", [])
    except json.JSONDecodeError:
        print(f"[triage] WARNING: unparseable model output: {raw[:200]}",
              file=sys.stderr)
        return []


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("transcripts", nargs="+",
                        help="SANITIZED transcript JSONL files")
    parser.add_argument("-o", "--output", required=True,
                        help="Output findings JSON file")
    args = parser.parse_args()

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("[triage] ERROR: DEEPSEEK_API_KEY not set. The reflection "
                 "skill may fall back to analyzing directly, but must note "
                 "the cost-guard bypass in the proposal PR.")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    results = []
    for name in args.transcripts:
        path = Path(name)
        print(f"[triage] Analyzing {path.name} with {model}...")
        for start, chunk in chunk_transcript(path):
            raw = call_deepseek(base_url, api_key, model, TRIAGE_SYSTEM_PROMPT,
                                f"Transcript: {path.name}\n\n{chunk}")
            for finding in parse_findings(raw):
                finding["transcript"] = path.name
                results.append(finding)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": model, "findings": results}, indent=2),
                   encoding="utf-8")
    print(f"[triage] {len(results)} finding(s) -> {out}")


if __name__ == "__main__":
    main()
