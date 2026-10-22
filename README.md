# session-local-search

Fast local search over every coding-agent session on a machine: Claude Code (every
account home), Codex and Antigravity. One SQLite file, Python standard library only.

```
sls search "dishwasher error code"          # best match first
sls search "kimi k3" --newest --agent claude
sls serve                                   # http://127.0.0.1:31096/ and the tailnet address
sls index                                   # catch up (cheap: reads only appended bytes)
sls index --optimize                        # the nightly pass
sls stats
```

## What it reads

| Agent | Where | What is indexed |
|---|---|---|
| Claude Code | `~/.claude*/projects/<folder>/<session>.jsonl`, every account home | prompts (also those typed mid-turn), replies, one line per tool call; titles (`custom-title` > `agent-name` > sessionpipe > `ai-title`) |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | user and assistant messages, one line per tool call |
| Antigravity | `~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl` | the user request, the planner's replies, tool calls |
| sessionpipe | `~/.local/state/sessionpipe/outbox/<harness>/<id>.jsonl` (30 days) | only the claude.ai link and title of each session |

Tool **output** is never indexed: it is most of the bytes and rarely what you look for.
Harness notices (`<system-reminder>`, task notifications) are skipped too. A session that
the session mover copied into a second account home is indexed once, from the larger file.

## How it stays fast

- **Incremental by byte offset.** Transcripts are append-only JSONL. Each file remembers how
  far it was read; the next pass parses only what was appended. A line still being written
  (no newline yet) waits. A file that shrank or was replaced is read again from the start.
- **Recent things are plain text search.** Whatever was appended since the last index pass
  is read and matched as text at query time (capped at 300 MB per query), so a session that
  started five minutes ago is found before any index has seen it. Those hits say *live*.
- **Overnight it indexes.** A nightly LaunchAgent runs `sls index --optimize`, and the server
  runs a niced catch-up when it starts if the index is more than 30 minutes old.

## Query syntax

Words are ANDed. `"a phrase"` stays a phrase, `-word` drops sessions that mention it,
`pre*` is a prefix. Matching is per message first; when that finds too few sessions, sessions
that contain every word somewhere (across messages) are added after them.

## The page

`sls serve` binds `127.0.0.1` and the machine's Tailscale address, never `0.0.0.0`: the page
shows private session text and has no login, so the tailnet (WireGuard, your own devices) is
the lock. Each result shows the session's title, agent, machine, account, folder, time span,
the hits with the words marked, the `claude --resume` line, and the claude.ai link when the
session ran under Remote Control. `/s/<session-id>` shows the whole indexed session.
`/api/search?q=` returns JSON.

## Install as LaunchAgents (macOS)

```
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yaroslavvb.session-local-search.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yaroslavvb.session-local-search-index.plist
```

The plists name absolute paths (this checkout and `/usr/local/bin/python3.12`); edit them for
another machine. The index lives at `~/.local/share/session-local-search/index.sqlite`
(`SLS_DB` overrides it) and never enters git.

## Settings

`SLS_DB` (index path), `SLS_PORT` (default 31096), `SLS_HOME` (whose sessions; tests use it),
`SLS_ACCOUNTS_TSV` (labels for account homes; default `~/git0/flush-util/claude_accounts.tsv`).

## Tests

```
python3 -m unittest discover -s tests
```

## Not yet

Other machines (the other Macs over ssh, the Linux lab boxes), ranking by recency as well as
match, the session board's digests, subagent transcripts, Devin.
