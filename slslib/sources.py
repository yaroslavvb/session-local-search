"""Where agent sessions live on this machine, and how to read the words out of them.

Three transcript formats are read directly, because they keep everything since the
harness was installed:

  Claude Code   ~/.claude*/projects/<folder>/<session>.jsonl  (one per account home)
  Codex         ~/.codex/sessions/YYYY/MM/DD/rollout-...-<id>.jsonl
  Antigravity   ~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl

sessionpipe's outbox (~/.local/state/sessionpipe/outbox/<harness>/<id>.jsonl) keeps only
30 days, but it is the one place that knows a Remote Control session's claude.ai link
and its title, so it is read for those two fields only (the tail of each file).

Only words a person or a model wrote are indexed: prompts, replies, and a one-line
summary of each tool call (the command, the path, the query). Tool OUTPUT is skipped:
it is most of the bytes (file dumps, logs) and almost never what one searches for.
"""

from __future__ import annotations

import glob
import json
import os
import re
import socket

MAX_TEXT = 20000          # chars kept per message; longer replies are rare
TOOL_SUMMARY = 300        # chars kept per tool call
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
REQUEST_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def home_dir() -> str:
    return os.environ.get("SLS_HOME") or os.path.expanduser("~")


def machine_name(home: str | None = None) -> str:
    """sessionpipe's machine name when it is set up (it is what the board shows), else the host."""
    home = home or home_dir()
    try:
        with open(os.path.join(home, ".config", "sessionpipe", "config.json")) as f:
            name = json.load(f).get("machine")
        if name:
            return str(name)
    except (OSError, ValueError):
        pass
    return socket.gethostname().split(".")[0]


def account_labels(home: str | None = None) -> dict[str, str]:
    """Map an account home (absolute path) to a short label.

    On his machines the labels come from flush-util's claude_accounts.tsv (yvb, yvb2,
    mlc, sheep, ...); anywhere else the folder name is the label.
    """
    home = home or home_dir()
    tsv = os.environ.get("SLS_ACCOUNTS_TSV") or os.path.join(home, "git0", "flush-util", "claude_accounts.tsv")
    labels: dict[str, str] = {}
    try:
        with open(tsv) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.rstrip("\n").split("\t")
                if len(cols) >= 7 and cols[2].startswith("~"):
                    labels[os.path.join(home, cols[2][2:])] = cols[6]
    except OSError:
        pass
    labels.setdefault(os.path.join(home, ".claude"), "shared")
    return labels


def label_for(acct_home: str, labels: dict[str, str]) -> str:
    if acct_home in labels:
        return labels[acct_home]
    base = os.path.basename(acct_home)
    return base[len(".claude-"):] if base.startswith(".claude-") else base.lstrip(".")


def discover(home: str | None = None) -> list[dict]:
    """Every transcript file, one per session.

    Claude homes can link into a shared projects tree, and the session mover copies a
    session from one home to another, so the same session can sit at several paths.
    Links are collapsed by realpath; copies by session id, keeping the largest file.
    """
    home = home or home_dir()
    found: dict[tuple[str, str], dict] = {}

    def add(path: str, harness: str, sid: str, acct_home: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        if os.path.islink(path):                 # rare: folders are resolved once, below
            path = os.path.realpath(path)
        key = (harness, sid)
        prev = found.get(key)
        if prev is None or st.st_size > prev["size"]:
            found[key] = {"path": path, "harness": harness, "session_id": sid,
                          "home": acct_home, "size": st.st_size, "mtime": st.st_mtime, "inode": st.st_ino}

    for proj in sorted(glob.glob(os.path.join(home, ".claude*", "projects"))):
        acct_home = os.path.dirname(proj)
        for folder in glob.glob(os.path.join(proj, "*")):
            real = os.path.realpath(folder)      # one realpath per folder, not per file
            try:
                names = os.listdir(real)
            except OSError:
                continue
            for name in names:
                if name.endswith(".jsonl"):
                    add(os.path.join(real, name), "claude", name[:-len(".jsonl")], acct_home)
    for p in glob.glob(os.path.join(home, ".codex", "sessions", "*", "*", "*", "*.jsonl")):
        m = UUID_RE.findall(os.path.basename(p))
        add(os.path.realpath(p) if os.path.islink(os.path.dirname(p)) else p, "codex",
            m[-1] if m else os.path.basename(p)[:-6], os.path.join(home, ".codex"))
    for p in glob.glob(os.path.join(home, ".gemini", "antigravity", "brain", "*", ".system_generated",
                                    "logs", "transcript.jsonl")):
        sid = p.split(os.sep + "brain" + os.sep, 1)[1].split(os.sep, 1)[0]
        add(os.path.realpath(p), "antigravity", sid, os.path.join(home, ".gemini"))

    # realpath can still collide when two homes link the same file under different ids
    by_path: dict[str, dict] = {}
    for rec in found.values():
        by_path.setdefault(rec["path"], rec)
    return sorted(by_path.values(), key=lambda r: r["mtime"])


def _clip(text: str, n: int = MAX_TEXT) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n] + " …"


PLACEHOLDER_RE = re.compile(r"\[(external )?unsupported block[^\]]*\]|\[Image #?\d*\]|\[image\]", re.I)
REQUEST_MARK = re.compile(r"^##\s*My request(?: for Codex)?:\s*$", re.M)


def clean_prompt(text: str) -> str:
    """Drop image placeholders, and lift the request out of Codex's attached-files preamble."""
    text = PLACEHOLDER_RE.sub("", text)
    if text.lstrip().startswith("# Files mentioned by the user"):
        m = REQUEST_MARK.search(text)
        if m:
            names = re.findall(r"^##\s*(.+?):\s*/", text[:m.start()], re.M)
            request = text[m.end():].strip()
            text = request + (f"\n(files: {', '.join(names)})" if names else "")
    return text.strip()


def tool_summary(name: str | None, inp) -> str:
    """One line per tool call: the part a person would search for."""
    if not name:
        return ""
    if isinstance(inp, dict):
        for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt",
                  "notebook_path", "skill", "subject"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return f"[{name}] " + _clip(v, TOOL_SUMMARY)
        return f"[{name}]"
    if isinstance(inp, str) and inp.strip():
        return f"[{name}] " + _clip(inp, TOOL_SUMMARY)
    return f"[{name}]"


def parse_claude(r: dict):
    """(kind, payload) for one Claude Code record: ('msg', (role, ts, text)) or ('meta', {...})."""
    t = r.get("type")
    if t == "user":
        if r.get("isMeta") or r.get("isSidechain"):
            return None
        c = (r.get("message") or {}).get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            parts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
            if not parts:
                return None                      # tool results
            text = "\n".join(parts)
        else:
            return None
        if text.lstrip().startswith("<"):
            return None                          # harness notices, slash-command wrappers
        text = clean_prompt(text)
        if not text:
            return None
        meta = {"cwd": r.get("cwd"), "branch": r.get("gitBranch")}
        return ("msg", ("user", r.get("timestamp"), _clip(text)), meta)
    if t == "assistant":
        if r.get("isSidechain"):
            return None
        parts, tools = [], []
        for x in (r.get("message") or {}).get("content") or []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "text" and x.get("text", "").strip():
                parts.append(x["text"])
            elif x.get("type") == "tool_use":
                s = tool_summary(x.get("name"), x.get("input"))
                if s:
                    tools.append(s)
        out = []
        if parts:
            out.append(("assistant", r.get("timestamp"), _clip("\n".join(parts))))
        if tools:
            out.append(("tool", r.get("timestamp"), _clip("\n".join(tools))))
        return ("msgs", out, {"cwd": r.get("cwd"), "branch": r.get("gitBranch")}) if out else None
    if t == "attachment":
        a = r.get("attachment") or {}
        if a.get("type") == "queued_command":
            text = a.get("prompt") if isinstance(a.get("prompt"), str) else ""
            if text.strip() and not text.lstrip().startswith("<"):
                return ("msg", ("user", r.get("timestamp"), _clip(text)), {})
        return None
    if t == "custom-title" and r.get("customTitle"):
        return ("title", r["customTitle"], 3)
    if t == "agent-name" and r.get("agentName"):
        return ("title", r["agentName"], 2)
    if t == "ai-title" and r.get("aiTitle"):
        return ("title", r["aiTitle"], 1)
    if t == "summary" and r.get("summary"):
        return ("title", r["summary"], 0)
    return None


def parse_codex(r: dict):
    t = r.get("type")
    p = r.get("payload") or {}
    if t == "session_meta":
        return ("meta", {"cwd": p.get("cwd"), "first_ts": p.get("timestamp") or r.get("timestamp")})
    if t == "turn_context" and p.get("cwd"):
        return ("meta", {"cwd": p.get("cwd")})
    if t == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
        parts = [x.get("text", "") for x in p.get("content") or [] if isinstance(x, dict)
                 and x.get("type") in ("input_text", "output_text", "text")]
        text = "\n".join(s for s in parts if s.strip())
        if not text.strip() or text.lstrip().startswith("<") or text.startswith("# AGENTS.md"):
            return None
        text = clean_prompt(text) if p["role"] == "user" else text
        if not text:
            return None
        return ("msg", (p["role"], r.get("timestamp"), _clip(text)), {})
    if t == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
        inp = p.get("arguments") or p.get("input") or p.get("action")
        if isinstance(inp, str):
            try:
                inp = json.loads(inp)
            except ValueError:
                pass
        s = tool_summary(p.get("name") or p.get("type"), inp)
        return ("msg", ("tool", r.get("timestamp"), s), {}) if s else None
    return None


def parse_antigravity(r: dict):
    t = r.get("type")
    c = r.get("content")
    if t == "USER_INPUT" and isinstance(c, str):
        m = REQUEST_RE.search(c)
        text = m.group(1) if m else c
        return ("msg", ("user", r.get("created_at"), _clip(text)), {}) if text.strip() else None
    if t == "PLANNER_RESPONSE":
        out = []
        if isinstance(c, str) and c.strip():
            out.append(("assistant", r.get("created_at"), _clip(c)))
        tools = [tool_summary(x.get("name"), x.get("args")) for x in r.get("tool_calls") or []
                 if isinstance(x, dict)]
        tools = [s for s in tools if s]
        if tools:
            out.append(("tool", r.get("created_at"), _clip("\n".join(tools))))
        return ("msgs", out, {}) if out else None
    return None


PARSERS = {"claude": parse_claude, "codex": parse_codex, "antigravity": parse_antigravity}


def read_from(path: str, harness: str, offset: int):
    """Parse complete lines from `offset`; return (records, new_offset).

    A line the harness is still writing (no newline yet) is left for next time, which
    is what makes the index safe to run against live sessions.
    """
    parse = PARSERS[harness]
    out = []
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b"\n"):
                break
            pos += len(raw)
            if harness == "claude" and b'"type":"tool_result"' in raw:
                continue                         # tool results: the bulk of the bytes
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            got = parse(r)
            if got:
                out.append(got)
    return out, pos


def outbox_meta(home: str | None = None) -> dict[str, dict]:
    """claude.ai link and title per session id, from the tail of sessionpipe's outbox files."""
    home = home or home_dir()
    root = os.path.join(home, ".local", "state", "sessionpipe", "outbox")
    meta: dict[str, dict] = {}
    for p in glob.glob(os.path.join(root, "*", "*.jsonl")):
        sid = os.path.basename(p)[:-len(".jsonl")]
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as f:
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        url = title = None
        for line in reversed(tail.splitlines()):
            if '"session"' not in line:
                continue
            try:
                s = json.loads(line).get("session") or {}
            except ValueError:
                continue
            url = url or s.get("url")
            title = title or s.get("title")
            if url and title:
                break
        if url or title:
            meta[sid] = {"url": url, "title": title}
    return meta
