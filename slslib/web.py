"""A small search page for phone and desktop: one box, results grouped by session.

Served on loopback and on the tailnet address only, never 0.0.0.0: the page shows
private session text and has no login, so the network has to be the lock. Tailscale
is WireGuard-authenticated and holds only the owner's own devices.
"""

from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store

TAILNET = ipaddress.ip_network("100.64.0.0/10")
STYLE = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#6b6a65;--card:#fff;--line:#e4e1d8;--acc:#8a5a00;--mark:#ffe9a8}
@media (prefers-color-scheme: dark){:root{--bg:#141413;--fg:#ecebe6;--mute:#a19f97;--card:#1d1d1b;
--line:#2f2e2b;--acc:#e0b04a;--mark:#5a4410}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:900px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 12px}h1 a{color:inherit;text-decoration:none}
form{display:flex;gap:8px;flex-wrap:wrap}
input[type=search]{flex:1 1 260px;font-size:17px;padding:10px 12px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg)}
select,button{font-size:15px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg)}
.meta{color:var(--mute);font-size:13px}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px;margin:12px 0}
.card h2{font-size:16px;margin:0 0 4px}.card h2 a{color:var(--acc);text-decoration:none}
.hit{margin:8px 0 0;padding:6px 0 0;border-top:1px dashed var(--line);overflow-wrap:anywhere}
.role{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin-right:6px}
mark{background:var(--mark);color:inherit;padding:0 1px;border-radius:2px}
code{font:12px ui-monospace,Menlo,monospace;background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:4px 6px;display:block;overflow-x:auto;white-space:pre;margin-top:6px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:8px 0;
white-space:pre-wrap;overflow-wrap:anywhere}.msg.user{border-left:4px solid var(--acc)}
.live{color:var(--acc);font-size:12px;margin-left:6px}
"""


def _local(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%a %d %b %Y %H:%M")
    except ValueError:
        return ts


def _snip(s: str) -> str:
    return html.escape(s or "").replace("\x02", "<mark>").replace("\x03", "</mark>")


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
            f"<body><main>{body}</main></body></html>").encode()


def _form(q: str = "", sort: str = "relevance", harness: str = "") -> str:
    opt = lambda v, label, cur: f"<option value='{v}'{' selected' if v == cur else ''}>{label}</option>"
    return ("<form action='/' method=get>"
            f"<input type=search name=q value='{html.escape(q, quote=True)}' placeholder='words, \"a phrase\", -not, pre*' autofocus>"
            f"<select name=sort>{opt('relevance', 'best match', sort)}{opt('newest', 'newest', sort)}</select>"
            f"<select name=harness>{opt('', 'all agents', harness)}{opt('claude', 'Claude Code', harness)}"
            f"{opt('codex', 'Codex', harness)}{opt('antigravity', 'Antigravity', harness)}</select>"
            "<button>Search</button></form>")


_STATS = {"at": 0.0, "v": None}


def cached_stats() -> dict:
    """The header's counts, re-read at most once a minute (a count over every message is not free)."""
    if _STATS["v"] is None or time.time() - _STATS["at"] > 60:
        _STATS["v"], _STATS["at"] = store.stats(), time.time()
    return _STATS["v"]


def render_results(res: dict, q: str, sort: str, harness: str) -> bytes:
    st = cached_stats()
    head = (f"<h1><a href='/'>Session search</a></h1>{_form(q, sort, harness)}"
            f"<p class=meta>{st['sessions']:,} sessions · {st['messages']:,} messages indexed · "
            f"last index {_local(_iso(st.get('last_index_end')))}</p>")
    if not q:
        return _page("Session search", head)
    if res.get("error"):
        return _page("Session search", head + f"<p>Query error: {html.escape(res['error'])}</p>")
    lv = res.get("live") or {}
    parts = [head, f"<p class=meta>{len(res['sessions'])} sessions in {res['ms']} ms"
                   f" (plus a plain-text pass over {lv.get('files', 0)} files changed since the last index)</p>"]
    for s in res["sessions"]:
        sid = s["session_id"]
        title = s.get("title") or (s.get("first_prompt") or "")[:90] or sid
        link = f"/s/{urllib.parse.quote(sid)}?q={urllib.parse.quote(q)}"
        bits = [s.get("harness"), s.get("machine"), s.get("account"), s.get("cwd"),
                f"{_local(s.get('first_ts'))} → {_local(s.get('last_ts'))}", f"{s.get('n_msgs') or 0} msgs"]
        meta = " · ".join(html.escape(str(b)) for b in bits if b)
        url = f" · <a href='{html.escape(s['url'], quote=True)}'>open on claude.ai</a>" if s.get("url") else ""
        hits = "".join(
            f"<div class=hit><span class=role>{html.escape(h['role'] or '')}</span>"
            f"<span class=meta>{_local(h.get('ts'))}</span>{'<span class=live>live</span>' if h.get('live') else ''}"
            f"<div>{_snip(h.get('snippet'))}</div></div>" for h in s.get("hits") or [])
        parts.append(f"<div class=card><h2><a href='{link}'>{html.escape(title)}</a></h2>"
                     f"<div class=meta>{meta}{url}</div><code>{html.escape(s.get('resume') or '')}</code>{hits}</div>")
    return _page(f"{q} · session search", "".join(parts))


def render_session(sid: str, q: str) -> bytes:
    s, msgs = store.session_messages(sid)
    if not s:
        return _page("not found", f"<h1><a href='/'>Session search</a></h1><p>No session {html.escape(sid)}.</p>")
    terms = store.parse_query(q)[3]
    rx = re.compile("|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)), re.I) if terms else None
    title = s.get("title") or (s.get("first_prompt") or "")[:90] or sid
    url = f" · <a href='{html.escape(s['url'], quote=True)}'>open on claude.ai</a>" if s.get("url") else ""
    out = [f"<h1><a href='/'>Session search</a></h1>{_form(q)}",
           f"<div class=card><h2>{html.escape(title)}</h2><div class=meta>"
           f"{html.escape(' · '.join(str(b) for b in (s.get('harness'), s.get('machine'), s.get('account'), s.get('cwd'), sid) if b))}"
           f"{url}</div><code>{html.escape(s.get('resume') or '')}</code></div>"]
    first_hit = None
    for mid, role, ts, text in msgs:
        body = html.escape(text)
        if rx:
            body, n = rx.subn(lambda m: f"<mark>{m.group(0)}</mark>", body)
            if n and first_hit is None:
                first_hit = mid
        out.append(f"<div class='msg {html.escape(role)}' id=m{mid}><span class=role>{html.escape(role)}</span>"
                   f"<span class=meta>{_local(ts)}</span>\n{body}</div>")
    if first_hit:
        out.insert(2, f"<p class=meta><a href='#m{first_hit}'>jump to the first match</a></p>")
    return _page(title, "".join(out))


def _iso(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "session-local-search/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        q = (qs.get("q") or [""])[0]
        sort = (qs.get("sort") or ["relevance"])[0]
        harness = (qs.get("harness") or [""])[0]
        try:
            if u.path == "/healthz":
                return self._send(200, b"ok\n", "text/plain")
            if u.path == "/api/search":
                res = store.search(q, limit=int((qs.get("limit") or ["20"])[0]), sort=sort, harness=harness or None)
                return self._send(200, json.dumps(res).encode(), "application/json")
            if u.path == "/api/stats":
                return self._send(200, json.dumps(store.stats()).encode(), "application/json")
            if u.path.startswith("/s/"):
                return self._send(200, render_session(urllib.parse.unquote(u.path[3:]), q))
            if u.path == "/":
                res = store.search(q, sort=sort, harness=harness or None) if q else {}
                return self._send(200, render_results(res, q, sort, harness))
            return self._send(404, b"not found\n", "text/plain")
        except Exception as e:                   # a bad query must not kill the server
            return self._send(500, f"error: {e}\n".encode(), "text/plain")


def tailscale_ip() -> str | None:
    """This machine's tailnet address, read from the interface (no GUI or daemon needed).

    The Tailscale GUI CLI can print an error on stdout and exit 0, so any answer is
    validated as an address inside 100.64.0.0/10 before it is used.
    """
    def valid(text):
        try:
            ip = ipaddress.ip_address(text.strip())
        except ValueError:
            return None
        return str(ip) if ip in TAILNET else None
    for cmd in (["/sbin/ifconfig"], ["/sbin/ip", "-4", "addr"]):
        if not os.path.exists(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            if (ip := valid(m.group(1))):
                return ip
    for exe in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/local/bin/tailscale",
                "/opt/homebrew/bin/tailscale", "/usr/bin/tailscale"):
        if os.path.exists(exe):
            try:
                r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            for line in (r.stdout or "").splitlines():
                if (ip := valid(line)):
                    return ip
    return None


def serve(port: int, tailnet: bool = True, retry_s: float = 30.0) -> None:
    local = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"session search on http://127.0.0.1:{port}/", flush=True)

    def tailnet_loop():
        while True:
            ip = tailscale_ip()
            if ip:
                try:
                    srv = ThreadingHTTPServer((ip, port), Handler)
                except OSError as e:
                    print(f"cannot bind {ip}:{port} ({e}); retrying in {retry_s:.0f}s", flush=True)
                else:
                    print(f"session search on http://{ip}:{port}/ (tailnet)", flush=True)
                    srv.serve_forever()
                    return
            else:
                print(f"no tailnet address yet; loopback only, retrying in {retry_s:.0f}s", flush=True)
            time.sleep(retry_s)

    if tailnet:
        threading.Thread(target=tailnet_loop, daemon=True).start()
    local.serve_forever()
