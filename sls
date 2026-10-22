#!/usr/bin/env python3
"""sls — fast local search over Claude Code, Codex and Antigravity sessions. See README.md."""
import os
import sys

if sys.version_info < (3, 9):
    # macOS's /usr/bin/python3 can be 3.7 on an older Mac; find a newer one before failing.
    for exe in ("/usr/local/bin/python3.12", "/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.11",
                "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.13", "/opt/homebrew/bin/python3.13"):
        if os.path.exists(exe):
            os.execv(exe, [exe] + sys.argv)
    sys.exit("sls needs Python 3.9 or newer")

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from slslib import cli  # noqa: E402

sys.exit(cli.main(sys.argv[1:]))
