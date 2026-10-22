"""sls — search every agent session on this machine.

    sls index [--full] [--optimize]   bring the index up to date (nightly: --optimize)
    sls search "words" [-n 20] [--newest] [--agent claude|codex|antigravity] [--json]
    sls serve [--port 31096] [--no-tailnet]
    sls stats
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

from . import store, web

DEFAULT_PORT = int(os.environ.get("SLS_PORT", "31096"))
CATCHUP_AFTER_S = 1800


def _catch_up_in_background() -> None:
    """On server start, run a niced index pass if the last one is old: cheap when nothing changed."""
    con = store.connect()
    last = float(store.get_meta(con, "last_index_end", 0) or 0)
    con.close()
    if time.time() - last < CATCHUP_AFTER_S:
        return
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sls")
    cmd = ["nice", "-n", "19", sys.executable, entry, "index", "--quiet"]

    def run():
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=run, daemon=True).start()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sls", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("index", help="index new transcript lines")
    pi.add_argument("--full", action="store_true", help="rebuild from nothing")
    pi.add_argument("--optimize", action="store_true", help="merge the FTS index afterwards (nightly)")
    pi.add_argument("--quiet", action="store_true")
    ps = sub.add_parser("search", help="search")
    ps.add_argument("query", nargs="+")
    ps.add_argument("-n", "--limit", type=int, default=20)
    ps.add_argument("--newest", action="store_true", help="newest first instead of best match")
    ps.add_argument("--agent", choices=["claude", "codex", "antigravity"])
    ps.add_argument("--account", help="an account label, e.g. yvb2")
    ps.add_argument("--no-live", action="store_true", help="skip the plain-text pass over unindexed bytes")
    ps.add_argument("--json", action="store_true")
    pv = sub.add_parser("serve", help="the web page")
    pv.add_argument("--port", type=int, default=DEFAULT_PORT)
    pv.add_argument("--no-tailnet", action="store_true", help="loopback only")
    pv.add_argument("--no-catch-up", action="store_true")
    sub.add_parser("stats", help="what the index holds")
    a = ap.parse_args(argv)

    if a.cmd == "index":
        store.index(full=a.full, optimize=a.optimize, log=(lambda *_: None) if a.quiet else print)
        return 0
    if a.cmd == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    if a.cmd == "serve":
        if not a.no_catch_up:
            _catch_up_in_background()
        web.serve(a.port, tailnet=not a.no_tailnet)
        return 0
    res = store.search(" ".join(a.query), limit=a.limit, sort="newest" if a.newest else "relevance",
                       harness=a.agent, account=a.account, live=not a.no_live)
    if a.json:
        print(json.dumps(res, indent=2))
        return 0
    if res.get("error"):
        print("query error:", res["error"], file=sys.stderr)
        return 2
    lv = res.get("live") or {}
    print(f"{len(res['sessions'])} sessions, {res['ms']} ms (live pass: {lv.get('files', 0)} files)\n")
    for s in res["sessions"]:
        title = s.get("title") or (s.get("first_prompt") or "")[:80] or s["session_id"]
        print(f"■ {title}")
        print(f"  {s.get('harness')} · {s.get('account')} · {s.get('cwd') or ''} · {web._local(s.get('last_ts'))}")
        if s.get("url"):
            print(f"  {s['url']}")
        print(f"  {s['resume']}")
        for h in s["hits"]:
            snip = (h["snippet"] or "").replace("\x02", "«").replace("\x03", "»").replace("\n", " ")
            print(f"    {h['role']:9} {web._local(h['ts'])}{' live' if h['live'] else ''}  {snip[:220]}")
        print()
    return 0
