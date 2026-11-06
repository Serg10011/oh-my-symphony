"""Publish only the approved public OMS candidate or its checked delivery ref."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlencode

import api_candidate as candidate
import api_cloud_check as cloud
import api_session as session
import deliver

FORK = Path(__file__).resolve().parents[2]
BASE = "our-main"
CI_FILES = ("Dockerfile", "host.sh", "locked-requirements.py", "probe.py", "suite.sh")


class ReleaseError(session.SessionError):
    """Operator-facing reason, without Git output, credentials or file contents."""


def oid(repo, *args, data=None):
    return candidate._oid(candidate._git(repo, *args, data=data))


def commit(repo, tree, parents, title):
    # Content addressing makes retries reconstruct exactly the same object.
    # The timestamp is taken from a pinned parent, never the wall clock.
    stamp = candidate._git(repo, "show", "-s", "--format=%ct", parents[0]).strip()
    if not stamp.isdigit():
        raise ReleaseError("release_invalid_object")
    identity = b"Symphony <symphony@localhost> " + stamp + b" +0000"
    data = (f"tree {tree}\n" + "".join(f"parent {p}\n" for p in parents)).encode("ascii")
    data += b"author " + identity + b"\ncommitter " + identity + b"\n\n" + title.encode("ascii") + b"\n"
    return oid(repo, "hash-object", "-w", "-t", "commit", "--stdin", data=data)


def marker(repo, trial, *, fork=FORK):
    """A check ref has only reviewed CI files. The candidate is its sole parent."""
    if not cloud.OID.fullmatch(trial):
        raise ReleaseError("release_invalid_object")
    def blob(data):
        return oid(repo, "hash-object", "-w", "--no-filters", "--stdin", data=data)
    def tree(entries):
        rows = "".join(f"{mode} {kind} {sha}\t{name}\n" for name, mode, kind, sha in entries)
        return oid(repo, "mktree", data=rows.encode("ascii"))

    recipe = (fork / cloud.WORKFLOW).read_bytes()
    def digest(text):
        return hashlib.sha256(cloud.TRIAL_LINE.sub(rb"\g<1>TRIAL", text)).hexdigest()
    if digest(recipe) != cloud.RECIPE:
        raise ReleaseError("release_check_recipe_changed")
    recipe = recipe.replace(b"    branches: [codex/browser-api-pipeline]\n    paths:\n"
                            b"      - .github/workflows/symphony-cloud-check.yml\n      - ours/ci/linux/**\n",
                            b"    branches: ['codex/api-check/**']\n")
    recipe = recipe.replace(b"github.ref == 'refs/heads/codex/browser-api-pipeline'",
                            b"startsWith(github.ref, 'refs/heads/codex/api-check/')")
    recipe = cloud.TRIAL_LINE.sub(rb"\g<1>" + trial.encode("ascii"), recipe)
    if digest(recipe) != cloud.CHECK_RECIPE:
        raise ReleaseError("release_check_recipe_changed")
    ci = tree([(name, "100644", "blob", blob((fork / "ours/ci/linux" / name).read_bytes())) for name in CI_FILES])
    attrs = blob((fork / ".gitattributes").read_bytes())
    if ci != cloud.TOOLS_TREE or attrs != cloud.ATTRIBUTES:
        raise ReleaseError("release_check_recipe_changed")
    tools_tree = tree([("linux", "040000", "tree", ci)])
    ours = tree([("ci", "040000", "tree", tools_tree)])
    workflows = tree([(Path(cloud.WORKFLOW).name, "100644", "blob", blob(recipe))])
    github = tree([("workflows", "040000", "tree", workflows)])
    top = tree([(".gitattributes", "100644", "blob", attrs), (".github", "040000", "tree", github),
                ("ours", "040000", "tree", ours)])
    return commit(repo, top, [trial], "Check public Symphony candidate " + trial)


class Publisher(deliver.Delivery):
    """Reuse the operator's delivery branch, PR creation/recovery and receipt format."""

    def __init__(self, board, *, gh=None, ops=None):
        gh = gh or shutil.which("gh", path=deliver.SAFE_PATH + ":/opt/homebrew/bin:/usr/local/bin")
        if not gh or not deliver.SAFE_GH_RE.fullmatch(gh):
            raise ReleaseError("release_gh_unavailable")
        super().__init__(board.identifier, board.workflow.parent.parent, None, gh, ops or deliver.Ops(), lambda _: None)
        self.bare = board.root / "candidate.git"
        # The capture creates this bare repository itself. Refuse later
        # credential helpers, URL rewrites or includes in its local config.
        keys = candidate._git(self.bare, "config", "--local", "--no-includes", "--name-only", "--list").splitlines()
        plain = {b"core.repositoryformatversion", b"core.filemode", b"core.bare", b"core.precomposeunicode",
                 b"core.ignorecase", b"core.logallrefupdates"}
        if any(key.lower() not in plain for key in keys):
            raise ReleaseError("release_target_changed")
        self.url = self.ops.remote_url(cloud.REPO)
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="https",
                        GIT_NO_REPLACE_OBJECTS="1", GH_HOST="github.com", GH_PROMPT_DISABLED="1")

    def tip(self, branch):
        return self.remote_ref(self.bare, self.url, "refs/heads/" + branch)

    def github(self, path):
        response = self.ops.proc([self.gh, "api", "--hostname", "github.com", path],
                                 env=self.env, timeout=deliver.GH_TIMEOUT_S)
        if response.rc:
            raise ReleaseError("release_remote_unavailable")
        try:
            return json.loads(response.out)
        except ValueError:
            raise ReleaseError("release_remote_unavailable") from None

    def check_destination(self):
        repo = self.github("repos/" + cloud.REPO)
        if (not isinstance(repo, dict) or repo.get("id") != cloud.REPO_ID
                or repo.get("full_name") != cloud.REPO or repo.get("private") is not False):
            raise ReleaseError("release_destination_invalid")

    def existing_pr(self, repo, base, branch):
        # A closed PR is also history. A retry after a lost create response
        # must never create another one on the operator's behalf.
        query = urlencode({"state": "all", "base": base, "head": repo.split("/")[0] + ":" + branch})
        rows = self.github(f"repos/{repo}/pulls?{query}")
        if not isinstance(rows, list) or len(rows) > 1:
            raise ReleaseError("release_pr_changed")
        if not rows:
            return None
        if not isinstance(rows[0], dict) or rows[0].get("state") != "open":
            raise ReleaseError("release_pr_changed")
        return {"number": rows[0].get("number"), "url": rows[0].get("html_url")}

    def create_branch(self, branch, sha):
        # Empty expected tip is Git's create-only compare-and-swap. This
        # cannot replace a ref, even if it appeared since the preceding read.
        if (not cloud.OID.fullmatch(sha) or not (branch == deliver.deliver_branch(self.id, BASE)
                or (branch.startswith(cloud.CHECK_PREFIX) and cloud.OID.fullmatch(branch[len(cloud.CHECK_PREFIX):])))):
            raise ReleaseError("release_invalid_object")
        ref = "refs/heads/" + branch
        result = self.git(self.bare, "-c", "core.hooksPath=/dev/null", "-c", "http.followRedirects=false",
                          "push", "--no-follow-tags", "--recurse-submodules=no",
                          "--force-with-lease=" + ref + ":", self.url, sha + ":" + ref,
                          net=True, timeout=deliver.PUSH_TIMEOUT_S)
        if result.rc or self.tip(branch) != sha:
            raise ReleaseError("release_branch_unconfirmed")

    def write_record(self, board, rec):
        # Called by the legacy open_pr after finding or creating a PR. A name
        # alone can match a branch in a fork. Verify its actual repository and
        # commit before making a permanent claim that this delivery succeeded.
        pr = rec["pr"]
        if (type(pr.get("number")) is not int or pr["number"] <= 0
                or pr.get("url") != f"https://github.com/{cloud.REPO}/pull/{pr['number']}"):
            raise ReleaseError("release_pr_unconfirmed")
        actual = self.github(f"repos/{cloud.REPO}/pulls/{pr['number']}")
        try:
            for key, ref, sha in (("head", rec["branch"], rec["commit"]), ("base", rec["base"], rec["our_main"])):
                entry = actual[key]
                if (entry["repo"]["id"] != cloud.REPO_ID or entry["repo"]["full_name"] != cloud.REPO
                        or entry["ref"] != ref or entry["sha"] != sha):
                    raise ReleaseError("release_pr_changed")
            if (actual["number"] != pr["number"] or actual["html_url"] != pr["url"] or actual["state"] != "open"):
                raise ReleaseError("release_pr_changed")
        except (KeyError, ValueError, TypeError):
            raise ReleaseError("release_pr_unconfirmed") from None
        session._save(deliver.delivered_path(board, self.id), rec)
