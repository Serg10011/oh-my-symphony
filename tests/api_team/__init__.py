"""Shared offline helpers for the API-team card tests.

The spend summary lives in the browser module ``api-team-costs.js`` because the
card must reuse the amounts of the detailed report instead of recalculating a
price of its own. The checks here run that module in Node and compare its result
with the very report text the board returns. Nothing here touches the network,
credentials or a live board.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "symphony" / "web" / "static"
COSTS_MODULE = STATIC / "api-team-costs.js"
HOOKS = ROOT / "ours" / "hooks"

# Loads the browser module in an isolated context and answers with the spend
# summary for one JSON request on stdin. It never uses the network or a DOM.
_RUNNER = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const modulePath = process.argv[1];
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(modulePath, 'utf8'), sandbox, { filename: modulePath });
const costs = sandbox.SymphonyApiTeamCosts;
if (!costs || typeof costs.summary !== 'function') {
  console.error('api-team-costs.js did not publish summary()');
  process.exit(2);
}
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(costs.summary(request.text, request.session, request.profiles)));
"""


def node_binary() -> str | None:
    """Find Node without downloading anything; the browser tests ship one."""
    configured = os.environ.get("SYMPHONY_NODE")
    if configured and os.access(configured, os.X_OK):
        return configured
    found = shutil.which("node")
    if found:
        return found
    try:  # Playwright bundles a Node build next to its driver.
        import playwright  # noqa: F401  (optional dependency)
    except ImportError:
        return None
    candidate = Path(playwright.__file__).resolve().parent / "driver" / "node"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None


def render_report(data: dict) -> str:
    """Render the detailed report with the board's own renderer, offline."""
    if str(HOOKS) not in sys.path:
        sys.path.insert(0, str(HOOKS))
    import api_session  # noqa: PLC0415  (ours/hooks is the board's source of truth)

    return api_session.render_report(data)


def spend_summary(text: str, session: dict | None, profiles: dict | None) -> dict:
    """Run the card module and return its summary exactly as the browser sees it."""
    node = node_binary()
    if node is None:
        raise RuntimeError("node is required to run api-team-costs.js")
    request = json.dumps({"text": text, "session": session, "profiles": profiles})
    completed = subprocess.run([node, "-e", _RUNNER, str(COSTS_MODULE)], input=request,
                               capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"api-team-costs.js failed: {completed.stderr.strip()[:2000]}")
    return json.loads(completed.stdout)
