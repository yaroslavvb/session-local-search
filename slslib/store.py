"""The index: SQLite FTS5 over every prompt, reply and tool call, plus a live scan of new bytes.

Indexing is incremental by byte offset. Transcripts are append-only JSONL, so each file
remembers how far it was read, and the next pass parses only what was appended (a file
that shrank or was replaced is read again from the start). The nightly pass is that same
walk over every file, followed by an FTS `optimize`; `--full` rebuilds from nothing.

Searching has two halves, which is the "recent things are just text search, and overnight
it indexes" shape: FTS5 answers for everything indexed, and the bytes appended since the
last pass are read and matched as plain text at query time.
"""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import time

from . import sources

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, harness TEXT, session_id TEXT, inode INTEGER,
  size INTEGER, mtime REAL, offset INTEGER, indexed_at REAL);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, harness TEXT, machine TEXT, home TEXT, account TEXT,
  cwd TEXT, branch TEXT, first_ts TEXT, last_ts TEXT, title TEXT, title_prio INTEGER DEFAULT -1,
  url TEXT, path TEXT, n_msgs INTEGER DEFAULT 0, first_prompt TEXT);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, session_id TEXT, path TEXT, role TEXT, ts TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS messages_path ON messages(path);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# Title sources, strongest first: a name he typed, the agent's name, sessionpipe's title,
# Claude's own generated title, an old-style summary. Below all of them: the first prompt.
PRIO = {"custom": 30, "agent": 20, "outbox": 15, "ai": 10, "summary": 5}
CLAUDE_TITLE_PRIO = {3: PRIO["custom"], 2: PRIO["agent"], 1: PRIO["ai"], 0: PRIO["summary"]}
LIVE_BYTES_CAP = 300 * 1024 * 1024


def db_path() -> str:
    return os.environ.get("SLS_DB") or os.path.join(
        sources.home_dir(), ".local", "share", "session-local-search", "index.sqlite")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def get_meta(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))


def _refresh_session(con, sid):
    n, first, last = con.execute(
        "SELECT count(*), min(ts), max(ts) FROM messages WHERE session_id=?", (sid,)).fetchone()
    fp = con.execute("SELECT text FROM messages WHERE session_id=? AND role='user' ORDER BY ts, id LIMIT 1",
                     (sid,)).fetchone()
    con.execute("UPDATE sessions SET n_msgs=?, first_ts=coalesce(first_ts, ?), last_ts=?, first_prompt=? "
                "WHERE session_id=?", (n, first, last, fp[0][:400] if fp else None, sid))


def index(full: bool = False, optimize: bool = False, log=print, path: str | None = None) -> dict:
    """Bring the index up to date with every transcript on this machine.

    One pass at a time: the nightly agent, the server's catch-up and a manual run would
    otherwise read the same appended bytes and insert them twice. A second caller skips.
    """
    p = path or db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lock = open(p + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        log("another index pass is running; skipped")
        return {"skipped": True}
    try:
        return _index(full, optimize, log, path)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _index(full: bool, optimize: bool, log, path: str | None) -> dict:
    if full:                                     # a rebuild starts from an empty file
        p = path or db_path()
        for x in (p, p + "-wal", p + "-shm"):
            if os.path.exists(x):
                os.remove(x)
    con = connect(path)
    t0 = time.time()
    set_meta(con, "last_index_start", t0)
    labels = sources.account_labels()
    machine = sources.machine_name()
    known = {r[0]: r for r in con.execute("SELECT path, inode, size, mtime, offset FROM files")}
    files = sources.discover()
    touched = new_msgs = 0
    last_commit = time.time()
    for rec in files:
        path_, harness, sid = rec["path"], rec["harness"], rec["session_id"]
        k = known.get(path_)
        if k and k[1] == rec["inode"] and k[2] == rec["size"] and abs(k[3] - rec["mtime"]) < 1e-3:
            continue
        offset = 0
        if k and k[1] == rec["inode"] and rec["size"] >= (k[4] or 0):
            offset = k[4] or 0
        elif k:
            con.execute("DELETE FROM messages WHERE path=?", (path_,))
        try:
            records, pos = sources.read_from(path_, harness, offset)
        except OSError as e:
            log(f"skip {path_}: {e}")
            continue
        con.execute(
            "INSERT INTO sessions(session_id, harness, machine, home, account, path) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET path=excluded.path, home=excluded.home, "
            "account=excluded.account, harness=excluded.harness",
            (sid, harness, machine, rec["home"], sources.label_for(rec["home"], labels), path_))
        rows, cwd, branch, first_ts = [], None, None, None
        title, title_prio = None, -1
        for got in records:
            kind = got[0]
            if kind == "msg":
                role, ts, text = got[1]
                rows.append((sid, path_, role, ts, text))
                meta = got[2] if len(got) > 2 else {}
            elif kind == "msgs":
                for role, ts, text in got[1]:
                    rows.append((sid, path_, role, ts, text))
                meta = got[2]
            elif kind == "meta":
                meta = got[1]
                first_ts = first_ts or meta.get("first_ts")
            elif kind == "title":
                p = CLAUDE_TITLE_PRIO.get(got[2], 0)
                if p >= title_prio:
                    title, title_prio = got[1], p
                continue
            else:
                continue
            cwd = cwd or (meta or {}).get("cwd")
            branch = branch or (meta or {}).get("branch")
        if rows:
            con.executemany("INSERT INTO messages(session_id, path, role, ts, text) VALUES(?,?,?,?,?)", rows)
            new_msgs += len(rows)
        if cwd or branch or first_ts:
            con.execute("UPDATE sessions SET cwd=coalesce(cwd, ?), branch=coalesce(branch, ?), "
                        "first_ts=coalesce(first_ts, ?) WHERE session_id=?", (cwd, branch, first_ts, sid))
        if title and sources.clean_prompt(title):
            con.execute("UPDATE sessions SET title=?, title_prio=? WHERE session_id=? AND title_prio<=?",
                        (sources.clean_prompt(title)[:300], title_prio, sid, title_prio))
        _refresh_session(con, sid)
        con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)",
                    (path_, harness, sid, rec["inode"], rec["size"], rec["mtime"], pos, time.time()))
        touched += 1
        if time.time() - last_commit > 5:
            con.commit()
            last_commit = time.time()
    for sid, m in sources.outbox_meta().items():
        if m.get("url"):
            con.execute("UPDATE sessions SET url=? WHERE session_id=?", (m["url"], sid))
        t = sources.clean_prompt(m.get("title") or "")
        if t:
            con.execute("UPDATE sessions SET title=?, title_prio=? WHERE session_id=? AND title_prio<?",
                        (t[:300], PRIO["outbox"], sid, PRIO["outbox"]))
    if optimize:
        con.execute("INSERT INTO messages_fts(messages_fts) VALUES('optimize')")
    t1 = time.time()
    set_meta(con, "last_index_end", t1)
    set_meta(con, "last_index_seconds", round(t1 - t0, 1))
    con.commit()
    con.close()
    res = {"files_seen": len(files), "files_read": touched, "new_messages": new_msgs, "seconds": round(t1 - t0, 1)}
    log(f"indexed: {res}")
    return res


# ---------- searching ----------

TOKEN_RE = re.compile(r'-?"[^"]*"|\S+')
WORD_RE = re.compile(r"\w+", re.UNICODE)


def parse_query(q: str):
    """Words are ANDed; "a phrase" stays a phrase; -word drops sessions that mention it; word* is a prefix.

    Returns (fts, positives, negatives, terms, negterms): `fts` ANDs the positive phrases
    for a per-message match, and the lists let the caller widen to whole sessions.
    """
    pos, neg, terms = [], [], []
    for tok in TOKEN_RE.findall(q or ""):
        is_neg = tok.startswith("-") and len(tok) > 1
        if is_neg:
            tok = tok[1:]
        prefix = tok.endswith("*") and not tok.startswith('"')
        body = tok.strip('"').rstrip("*")
        words = WORD_RE.findall(body)
        if not words:
            continue
        phrase = '"' + " ".join(words) + '"' + ("*" if prefix else "")
        (neg if is_neg else pos).append(phrase)
        if not is_neg:
            terms.append(" ".join(words))
    negterms = [" ".join(WORD_RE.findall(n)) for n in neg]
    if not pos:
        return None, [], neg, [], negterms
    return " AND ".join(pos), pos, neg, terms, negterms


def session_rows(con, sids):
    if not sids:
        return {}
    q = ",".join("?" * len(sids))
    cols = ["session_id", "harness", "machine", "home", "account", "cwd", "branch", "first_ts", "last_ts",
            "title", "url", "path", "n_msgs", "first_prompt"]
    out = {}
    for r in con.execute(f"SELECT {', '.join(cols)} FROM sessions WHERE session_id IN ({q})", list(sids)):
        out[r[0]] = dict(zip(cols, r))
    return out


def resume_line(s: dict) -> str:
    sid, home = s.get("session_id"), s.get("home") or ""
    h = sources.home_dir()
    short = home.replace(h, "~", 1) if home.startswith(h) else home
    if s.get("harness") == "claude":
        cd = f"cd {s['cwd']!r} && " if s.get("cwd") else ""
        return f"{cd}CLAUDE_CONFIG_DIR={short} claude --resume {sid}"
    if s.get("harness") == "codex":
        return f"codex resume {sid}"
    return f"open the Antigravity conversation {sid}"


SESSION_SET_CAP = 20000
WIDEN_BELOW = 5          # widen to whole sessions only when messages alone found fewer than this


def _sessions_matching(con, phrase: str) -> set:
    return {r[0] for r in con.execute(
        "SELECT DISTINCT m.session_id FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
        "WHERE messages_fts MATCH ? LIMIT ?", (phrase, SESSION_SET_CAP))}


def search(q: str, limit: int = 20, per_session: int = 3, sort: str = "relevance",
           harness: str | None = None, account: str | None = None, live: bool = True,
           path: str | None = None) -> dict:
    """Sessions whose messages match the query, best first.

    First pass: messages holding every word. If that finds fewer than `limit` sessions,
    sessions holding every word somewhere (across messages) are added after them.
    A -word drops any session that mentions it.
    """
    t0 = time.time()
    fts, pos, neg, terms, negterms = parse_query(q)
    if not fts:
        return {"query": q, "sessions": [], "ms": 0, "live": {"files": 0, "bytes": 0}}
    con = connect(path)
    where, args = ["messages_fts MATCH ?"], [fts]
    if harness:
        where.append("s.harness = ?")
        args.append(harness)
    if account:
        where.append("s.account = ?")
        args.append(account)
    order = "m.ts DESC" if sort == "newest" else "messages_fts.rank"
    base = ("SELECT m.id, m.session_id, m.role, m.ts, "
            "snippet(messages_fts, 0, char(2), char(3), ' … ', 24), messages_fts.rank "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "JOIN sessions s ON s.session_id = m.session_id ")
    try:
        dropped = set()
        for n in neg:
            dropped |= _sessions_matching(con, n)
        rows = con.execute(base + f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?",
                           args + [max(200, limit * per_session * 10)]).fetchall()
        groups: dict[str, list] = {}
        order_sids: list[str] = []
        for mid, sid, role, ts, snip, rank in rows:
            if sid in dropped:
                continue
            if sid not in groups:
                if len(order_sids) >= limit:
                    continue
                groups[sid] = []
                order_sids.append(sid)
            if len(groups[sid]) < per_session:
                groups[sid].append({"id": mid, "role": role, "ts": ts, "snippet": snip, "live": False})
        if len(order_sids) < min(limit, WIDEN_BELOW) and len(pos) > 1:
            counts = {ph: con.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH ?",
                                      (ph,)).fetchone()[0] for ph in pos}
            common = None
            for phrase in sorted(pos, key=counts.get):   # rarest first: empty sets end it early
                got = _sessions_matching(con, phrase)
                common = got if common is None else common & got
                if not common:
                    break
            extra = [sid for sid in (common or ()) if sid not in groups and sid not in dropped]
            if extra:
                meta = session_rows(con, extra)
                extra = [sid for sid in extra
                         if (not harness or meta.get(sid, {}).get("harness") == harness)
                         and (not account or meta.get(sid, {}).get("account") == account)]
                extra.sort(key=lambda sid: meta.get(sid, {}).get("last_ts") or "", reverse=True)
                extra = extra[:limit - len(order_sids)]
                best: dict[str, list] = {sid: [] for sid in extra}
                marks = ",".join("?" * len(extra))
                for phrase in pos[:per_session]:     # one query per word, not per session
                    seen = set()
                    for r in con.execute(base + f"WHERE messages_fts MATCH ? AND m.session_id IN ({marks}) "
                                         "ORDER BY messages_fts.rank", [phrase] + extra):
                        if r[1] in seen:
                            continue
                        seen.add(r[1])
                        best[r[1]].append({"id": r[0], "role": r[2], "ts": r[3], "snippet": r[4], "live": False})
                        if len(seen) == len(extra):
                            break
                for sid in extra:
                    groups[sid] = best[sid]
                    order_sids.append(sid)
    except sqlite3.OperationalError as e:
        con.close()
        return {"query": q, "error": str(e), "sessions": [], "ms": round((time.time() - t0) * 1000)}
    live_info = {"files": 0, "bytes": 0}
    if live:
        live_hits, live_info = live_search(con, terms, negterms, harness=harness)
        for sid, hits in live_hits.items():
            if sid not in groups:
                groups[sid] = []
                order_sids.insert(0, sid)        # new and unindexed: show it first
            groups[sid] = (hits + groups[sid])[:per_session]
    meta = session_rows(con, order_sids)
    con.close()
    out = []
    for sid in order_sids:
        s = meta.get(sid) or {"session_id": sid}
        if account and s.get("account") not in (None, account):
            continue
        s = dict(s)
        s["resume"] = resume_line(s)
        s["hits"] = groups[sid]
        out.append(s)
    return {"query": q, "fts": fts, "sessions": out[:limit + 5], "ms": round((time.time() - t0) * 1000),
            "live": live_info}


def _live_snippet(text: str, terms: list[str], width: int = 160) -> str:
    low = text.lower()
    at = min([i for i in (low.find(t.lower()) for t in terms) if i >= 0] or [0])
    start = max(0, at - width // 2)
    piece = text[start:start + width]
    for t in sorted(terms, key=len, reverse=True):
        piece = re.sub(re.escape(t), lambda m: "\x02" + m.group(0) + "\x03", piece, flags=re.I)
    return ("… " if start else "") + piece.replace("\n", " ") + (" …" if start + width < len(text) else "")


_DISCOVER: dict[str, tuple[float, list]] = {}


def discover_cached(max_age: float = 20.0):
    """The file walk, reused for a few seconds per home: a server answers many queries a minute."""
    home = sources.home_dir()
    at, v = _DISCOVER.get(home, (0.0, None))
    if v is None or time.time() - at > max_age:
        v = sources.discover()
        _DISCOVER[home] = (time.time(), v)
    return v


def live_search(con, terms, negs, harness=None):
    """Plain-text match over the bytes no index pass has read yet."""
    known = {r[0]: r for r in con.execute("SELECT path, inode, size, mtime, offset FROM files")}
    labels, machine = sources.account_labels(), sources.machine_name()
    hits: dict[str, list] = {}
    scanned = nfiles = 0
    lt = [t.lower() for t in terms]
    ln = [n.lower() for n in negs if n]
    for rec in reversed(discover_cached()):      # newest first
        if harness and rec["harness"] != harness:
            continue
        k = known.get(rec["path"])
        if k and k[1] == rec["inode"] and k[2] == rec["size"]:
            continue
        offset = k[4] if (k and k[1] == rec["inode"] and rec["size"] >= (k[4] or 0)) else 0
        if scanned + (rec["size"] - offset) > LIVE_BYTES_CAP:
            break
        scanned += rec["size"] - offset
        nfiles += 1
        try:
            records, _ = sources.read_from(rec["path"], rec["harness"], offset)
        except OSError:
            continue
        for got in records:
            msgs = [got[1]] if got[0] == "msg" else (got[1] if got[0] == "msgs" else [])
            for role, ts, text in msgs:
                low = text.lower()
                if all(t in low for t in lt) and not any(n in low for n in ln):
                    hits.setdefault(rec["session_id"], []).append(
                        {"id": None, "role": role, "ts": ts, "snippet": _live_snippet(text, terms), "live": True})
        if rec["session_id"] in hits:
            hits[rec["session_id"]] = sorted(hits[rec["session_id"]], key=lambda h: h["ts"] or "", reverse=True)
            con.execute(
                "INSERT OR IGNORE INTO sessions(session_id, harness, machine, home, account, path) "
                "VALUES(?,?,?,?,?,?)", (rec["session_id"], rec["harness"], machine, rec["home"],
                                        sources.label_for(rec["home"], labels), rec["path"]))
    con.commit()
    return hits, {"files": nfiles, "bytes": scanned}


def session_messages(sid: str, limit: int = 3000, path: str | None = None):
    con = connect(path)
    s = session_rows(con, [sid]).get(sid)
    msgs = con.execute("SELECT id, role, ts, text FROM messages WHERE session_id=? ORDER BY ts, id LIMIT ?",
                       (sid, limit)).fetchall()
    con.close()
    if s:
        s["resume"] = resume_line(s)
    return s, msgs


def stats(path: str | None = None) -> dict:
    p = path or db_path()
    con = connect(p)
    by = dict(con.execute("SELECT harness, count(*) FROM sessions GROUP BY harness").fetchall())
    msgs = con.execute("SELECT count(*) FROM messages").fetchone()[0]
    out = {
        "db": p,
        "db_mb": round(sum(os.path.getsize(x) for x in (p, p + "-wal") if os.path.exists(x)) / 1e6, 1),
        "sessions": sum(by.values()), "by_harness": by, "messages": msgs,
        "with_link": con.execute("SELECT count(*) FROM sessions WHERE url IS NOT NULL").fetchone()[0],
        "last_index_end": get_meta(con, "last_index_end"),
        "last_index_seconds": get_meta(con, "last_index_seconds"),
    }
    con.close()
    return out
