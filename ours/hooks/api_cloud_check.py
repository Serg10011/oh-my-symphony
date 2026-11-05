"""Read evidence from one reviewed Linux workflow in the public Symphony fork.

Only the operator's run number and values previously returned by GitHub enter
HTTP paths. Card data and local Git object names never leave the host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx

PROFILE = "oms-public-linux-v1"
REPO = "Serg10011/oh-my-symphony"
REPO_ID = 1431130186
WORKFLOW_ID = 408070416
BRANCH = "codex/browser-api-pipeline"
WORKFLOW = ".github/workflows/symphony-cloud-check.yml"
OID = re.compile(r"[0-9a-f]{40}")
TRIAL_LINE = re.compile(rb"(?m)^(      TRIAL_SHA: )([0-9a-f]{40})$")
# Reviewed at 22bdec0. Only the trial input may vary. The comparison base,
# dependency lock, summarizer source, commands and permissions stay pinned.
RECIPE = "61ca6f311b15c0f00a1c1cc931097526519aedca5a8527c48e95c17e187b65f9"
TOOLS_TREE = "75538fbd39a5f1abbda09a2fa3c6257843dfa717"
ATTRIBUTES = "7a3c5f6b686e3f0135b72b5f2581ddf4801e1c17"
STEPS = ("Set up job", "Prepare public revisions and trusted test tools",
         "Verify private loopback and denied outbound access", "Run installed base suite",
         "Run development suite", "Release test container", "Complete job")


class CloudError(Exception):
    """Fixed public reason, never a provider response body."""


def _require(condition):
    if not condition:
        raise CloudError("cloud_untrusted")


def _oid(value):
    _require(isinstance(value, str) and OID.fullmatch(value))
    return value


def run_number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise CloudError("cloud_invalid_run")
    return int(value)


def _get(client, path):
    with client.stream("GET", f"https://api.github.com/repos/{REPO}/{path}") as response:
        if response.status_code != 200:
            raise CloudError("cloud_unavailable")
        data = bytearray()
        for part in response.iter_bytes(65536):
            if len(data) + len(part) > 2 * 1024 * 1024:
                raise CloudError("cloud_untrusted")
            data.extend(part)
    value = json.loads(data)
    _require(isinstance(value, dict))
    return value


def _trial(get, head):
    # git archive also honours ancestor .gitattributes. Pin them together
    # with the tools tree so export-ignore/subst cannot alter the test recipe.
    tree = get(f"git/trees/{head}?recursive=1")
    _require(tree["truncated"] is False)
    entries = {item["path"]: (item["mode"], item["sha"]) for item in tree["tree"]}
    _require(len(entries) == len(tree["tree"])
             and entries.get("ours/ci/linux") == ("040000", TOOLS_TREE)
             and entries.get(".gitattributes") == ("100644", ATTRIBUTES)
             and "ours/.gitattributes" not in entries and "ours/ci/.gitattributes" not in entries)
    mode, oid = entries[WORKFLOW]
    _require(mode == "100644")
    blob = get(f"git/blobs/{_oid(oid)}")
    _require(blob["sha"] == oid and blob["encoding"] == "base64" and 0 < blob["size"] <= 8192)
    raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
    matches = TRIAL_LINE.findall(raw)
    _require(len(matches) == 1 and len(raw) == blob["size"]
             and hashlib.sha256(TRIAL_LINE.sub(rb"\g<1>TRIAL", raw)).hexdigest() == RECIPE)
    return matches[0][1].decode("ascii")


def _verify(get, number, commit, tree, parent):
    run = get(f"actions/runs/{number}")
    _require(run["id"] == number and run["workflow_id"] == WORKFLOW_ID and run["path"] == WORKFLOW
             and run["event"] in ("push", "workflow_dispatch") and run["head_branch"] == BRANCH)
    for repo in (run["repository"], run["head_repository"]):
        _require(repo["id"] == REPO_ID and repo["full_name"] == REPO and repo["private"] is False)
    if run["status"] != "completed" or run["conclusion"] not in ("success", "failure"):
        raise CloudError("cloud_incomplete")
    attempt, head = run["run_attempt"], _oid(run["head_sha"])
    _require(type(attempt) is int and attempt > 0)
    public_trial = _trial(get, head)
    if public_trial != commit:
        raise CloudError("cloud_other_candidate")
    tested = get(f"git/commits/{public_trial}")
    _require(tested["sha"] == public_trial)
    if tested["tree"]["sha"] != tree or [p["sha"] for p in tested["parents"]] != [parent]:
        raise CloudError("cloud_other_candidate")
    jobs = get(f"actions/runs/{number}/attempts/{attempt}/jobs")
    _require(jobs["total_count"] == len(jobs["jobs"]) == 1)
    job = jobs["jobs"][0]
    _require(job["name"] == "verify" and job["head_sha"] == head and job["run_id"] == number
             and job["run_attempt"] == attempt and type(job["id"]) is int and job["id"] > 0
             and job["status"] == "completed" and job["conclusion"] == run["conclusion"]
             # A private runner can register a label resembling a hosted one.
             # Pin GitHub's service-reported runner group for this public trial.
             and job["labels"] == ["ubuntu-24.04"] and type(job["runner_group_id"]) is int
             and job["runner_group_id"] == 0 and job["runner_group_name"] == "GitHub Actions")
    completed = job["completed_at"]
    _require(isinstance(completed, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z", completed))
    steps = job["steps"]
    _require(tuple(s["name"] for s in steps) == STEPS)
    outcomes = [s["conclusion"] for s in steps]
    if (any(s["status"] != "completed" for s in steps)
            or any(outcomes[i] != "success" for i in (0, 1, 2, 5, 6))
            or any(x not in ("success", "failure") for x in outcomes[3:5])):
        raise CloudError("cloud_incomplete")
    _require((run["conclusion"] == "success") == ("failure" not in outcomes))
    return {"run": number, "attempt": attempt, "job": job["id"], "completed_at": completed, "workflow_commit": head,
            "commit": commit, "tree": tree, "base_commit": parent, "comparison": outcomes[3],
            "status": "passed" if outcomes[4] == "success" else "failed"}


def verify(number, commit, tree, parent):
    number = run_number(number)
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=5,
                          headers={"Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "User-Agent": "Symphony-public-check"}) as client:
            return _verify(lambda path: _get(client, path), number, commit, tree, parent)
    except httpx.HTTPError:
        raise CloudError("cloud_unavailable") from None
    except (KeyError, TypeError, AttributeError, IndexError, ValueError, RecursionError):
        raise CloudError("cloud_untrusted") from None
