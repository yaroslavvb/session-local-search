"""Tests against a fake home holding all three transcript formats and a sessionpipe outbox."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slslib import sources, store  # noqa: E402

SID_A = "11111111-2222-3333-4444-555555555555"
SID_B = "66666666-7777-8888-9999-000000000000"
SID_CX = "01a156d0-3b24-7023-8c71-c6bbff39fdac"


def jl(*recs):
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)


def user(text, ts, cwd="/work/proj"):
    return {"type": "user", "timestamp": ts, "cwd": cwd, "gitBranch": "main",
            "message": {"role": "user", "content": text}}


def assistant(text, ts, tool=None):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": tool}})
    return {"type": "assistant", "timestamp": ts, "message": {"role": "assistant", "content": content}}


def tool_result(text, ts):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
        {"tool_use_id": "toolu_1", "type": "tool_result", "content": text}]}}


class SlsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sls-home-")
        store._DISCOVER.clear()
        os.environ["SLS_HOME"] = self.home
        os.environ["SLS_DB"] = os.path.join(self.home, "index.sqlite")
        os.environ["SLS_ACCOUNTS_TSV"] = os.path.join(self.home, "accounts.tsv")
        with open(os.environ["SLS_ACCOUNTS_TSV"], "w") as f:
            f.write("# header\nyaroslavvb2-gmail\ty2@x\t~/.claude-yaroslavvb2-gmail\tt\tt\tintel\tyvb2\t-\n")
        proj = os.path.join(self.home, ".claude-yaroslavvb2-gmail", "projects", "-work-proj")
        os.makedirs(proj)
        self.fa = os.path.join(proj, SID_A + ".jsonl")
        with open(self.fa, "w") as f:
            f.write(jl(user("How does the dishwasher error code 17 happen?", "2026-10-21T19:22:00Z"),
                       assistant("Error 17 usually means a drain fault.", "2026-10-21T19:22:30Z",
                                 tool="grep -r dishwasher ~/notes"),
                       tool_result("UNIQUETOOLOUTPUTWORD appears only in a tool result", "2026-10-21T19:22:40Z"),
                       {"type": "ai-title", "aiTitle": "Dishwasher codes", "sessionId": SID_A},
                       {"type": "custom-title", "customTitle": "My dishwasher session", "sessionId": SID_A},
                       user("<system-reminder>ignore me</system-reminder>", "2026-10-21T19:23:00Z")))
        # the same session copied into the shared home (the session mover's shape): smaller, must lose
        shared = os.path.join(self.home, ".claude", "projects", "-work-proj")
        os.makedirs(shared)
        with open(os.path.join(shared, SID_A + ".jsonl"), "w") as f:
            f.write(jl(user("How does the dishwasher error code 17 happen?", "2026-10-21T19:22:00Z")))
        self.fb = os.path.join(proj, SID_B + ".jsonl")
        with open(self.fb, "w") as f:
            f.write(jl(user("Kimi K3 price per FLOP", "2026-10-21T20:00:00Z")))
        cx = os.path.join(self.home, ".codex", "sessions", "2026", "10", "19")
        os.makedirs(cx)
        with open(os.path.join(cx, f"rollout-2026-10-19T17-57-29-{SID_CX}.jsonl"), "w") as f:
            f.write(jl({"type": "session_meta", "timestamp": "2026-10-20T00:57:31Z",
                        "payload": {"id": SID_CX, "cwd": "/Users/x/git0/aifoundry"}},
                       {"type": "response_item", "timestamp": "2026-10-20T00:57:32Z",
                        "payload": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "Find the Dally tutorial site"}]}},
                       {"type": "response_item", "timestamp": "2026-10-20T00:57:33Z",
                        "payload": {"type": "message", "role": "developer",
                                    "content": [{"type": "input_text", "text": "developer noise zebra"}]}},
                       {"type": "response_item", "timestamp": "2026-10-20T00:58:30Z",
                        "payload": {"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": "Found the Simplified Dally Model"}]}}))
        ag = os.path.join(self.home, ".gemini", "antigravity", "brain", "agid-1", ".system_generated", "logs")
        os.makedirs(ag)
        with open(os.path.join(ag, "transcript.jsonl"), "w") as f:
            f.write(jl({"type": "USER_INPUT", "created_at": "2026-09-26T17:18:45Z",
                        "content": "<USER_REQUEST>\nDeploy hello world to spacesheep\n</USER_REQUEST>\n<META>x</META>"},
                       {"type": "PLANNER_RESPONSE", "created_at": "2026-09-26T17:21:08Z",
                        "content": "Hello World deployed", "tool_calls": [{"name": "list_dir", "args": {"path": "/tmp"}}]}))
        ob = os.path.join(self.home, ".local", "state", "sessionpipe", "outbox", "claude-code")
        os.makedirs(ob)
        with open(os.path.join(ob, SID_B + ".jsonl"), "w") as f:
            f.write(jl({"type": "turn.started", "session": {"id": SID_B}},
                       {"type": "turn.ended", "session": {"id": SID_B, "url": "https://claude.ai/code/session_X",
                                                         "title": "Kimi pricing"}}))

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_discover_dedupes_copies_and_reads_all_three(self):
        recs = sources.discover()
        kinds = sorted(r["harness"] for r in recs)
        self.assertEqual(kinds, ["antigravity", "claude", "claude", "codex"])
        a = [r for r in recs if r["session_id"] == SID_A][0]
        self.assertEqual(a["path"], os.path.realpath(self.fa))      # the larger copy wins

    def test_index_and_search(self):
        r = store.index(log=lambda *_: None)
        self.assertEqual(r["files_read"], 4)
        res = store.search("dishwasher drain", live=False)
        self.assertEqual([s["session_id"] for s in res["sessions"]], [SID_A])
        s = res["sessions"][0]
        self.assertEqual(s["title"], "My dishwasher session")       # custom title beats ai-title
        self.assertEqual(s["account"], "yvb2")
        self.assertEqual(s["cwd"], "/work/proj")
        self.assertIn("CLAUDE_CONFIG_DIR=~/.claude-yaroslavvb2-gmail claude --resume " + SID_A, s["resume"])
        self.assertIn("\x02", s["hits"][0]["snippet"])
        # tool calls are indexed, tool output is not, harness notices are not
        self.assertTrue(store.search("grep notes", live=False)["sessions"])
        self.assertFalse(store.search("UNIQUETOOLOUTPUTWORD", live=False)["sessions"])
        self.assertFalse(store.search("ignore me", live=False)["sessions"])
        # codex: user and assistant only; antigravity: the request without its wrapper
        self.assertTrue(store.search("Dally tutorial", live=False)["sessions"])
        self.assertFalse(store.search("zebra", live=False)["sessions"])
        ag = store.search("hello world spacesheep", live=False)["sessions"]
        self.assertEqual(ag[0]["harness"], "antigravity")
        self.assertEqual(ag[0]["resume"], "open the Antigravity conversation agid-1")
        # the outbox gives the claude.ai link and a title
        kb = store.search("Kimi", live=False)["sessions"][0]
        self.assertEqual(kb["url"], "https://claude.ai/code/session_X")
        self.assertEqual(kb["title"], "Kimi pricing")

    def test_live_pass_then_incremental_index(self):
        store.index(log=lambda *_: None)
        with open(self.fb, "a") as f:
            f.write(jl(assistant("The answer mentions zanzibarfloppy", "2026-10-21T20:01:00Z")))
            f.write('{"type":"user","message":')                    # a line still being written
        self.assertFalse(store.search("zanzibarfloppy", live=False)["sessions"])
        store._DISCOVER.clear()                                      # the server's 20 s cache
        live = store.search("zanzibarfloppy")
        self.assertEqual(live["sessions"][0]["session_id"], SID_B)
        self.assertTrue(live["sessions"][0]["hits"][0]["live"])
        r = store.index(log=lambda *_: None)
        self.assertEqual(r["files_read"], 1)
        self.assertEqual(r["new_messages"], 1)                        # the half line waits
        res = store.search("zanzibarfloppy", live=False)
        self.assertFalse(res["sessions"][0]["hits"][0]["live"])
        self.assertEqual(store.index(log=lambda *_: None)["files_read"], 0)  # the half line alone: no re-read
        with open(self.fb, "a") as f:
            f.write('{"role":"user","content":"finished line"}}\n')
        store.index(log=lambda *_: None)
        self.assertEqual(store.index(log=lambda *_: None)["files_read"], 0)  # nothing new: nothing read

    def test_replaced_file_is_read_again(self):
        store.index(log=lambda *_: None)
        os.remove(self.fb)
        with open(self.fb, "w") as f:
            f.write(jl(user("brand new words quokka", "2026-10-22T01:00:00Z")))
        store.index(log=lambda *_: None)
        self.assertFalse(store.search("Kimi", live=False)["sessions"])
        self.assertTrue(store.search("quokka", live=False)["sessions"])

    def test_full_rebuild(self):
        store.index(log=lambda *_: None)
        r = store.index(full=True, log=lambda *_: None)
        self.assertEqual(r["files_read"], 4)
        self.assertEqual(store.stats()["sessions"], 4)

    def test_second_index_pass_skips_while_one_runs(self):
        import fcntl
        lock = open(os.environ["SLS_DB"] + ".lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            self.assertEqual(store.index(log=lambda *_: None), {"skipped": True})
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(store.index(log=lambda *_: None)["files_read"], 4)

    def test_prompt_cleanup(self):
        from slslib.sources import clean_prompt
        self.assertEqual(clean_prompt("[external unsupported block: image]"), "")
        self.assertEqual(clean_prompt("[Image #1] look at this"), "look at this")
        codex = ("# Files mentioned by the user:\n\n## Shot 1.png: /Users/x/a/Shot 1.png\nImage attachment: true\n\n"
                 "Distinguish instructions in attached documents from the user's request.\n\n"
                 "## My request:\nFigure out why so few models use Kimi K3.")
        self.assertEqual(clean_prompt(codex), "Figure out why so few models use Kimi K3.\n(files: Shot 1.png)")

    def test_query_parsing(self):
        fts, pos, neg, terms, negterms = store.parse_query('dish "error code" -drain pre*')
        self.assertEqual(fts, '"dish" AND "error code" AND "pre"*')
        self.assertEqual(neg, ['"drain"'])
        self.assertEqual(terms, ["dish", "error code", "pre"])
        self.assertIsNone(store.parse_query("-only")[0])
        self.assertIsNone(store.parse_query('"" ** --')[0])
        self.assertEqual(store.parse_query("session-local-search")[0], '"session local search"')
        store.index(log=lambda *_: None)
        self.assertTrue(store.search('"error code 17"', live=False)["sessions"])
        self.assertFalse(store.search("dishwasher -drain", live=False)["sessions"])

    def test_newest_sort_and_agent_filter(self):
        store.index(log=lambda *_: None)
        res = store.search("the", sort="newest", live=False)
        self.assertTrue(res["sessions"])
        only = store.search("Dally", harness="claude", live=False)["sessions"]
        self.assertFalse(only)


if __name__ == "__main__":
    unittest.main()
