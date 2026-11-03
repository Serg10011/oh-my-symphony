"""Read grants, immutable Git contents and bounded host operations."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ours/hooks"))
import api_project as project  # noqa: E402


def git(root, *args):
    command = [project.GIT, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
               "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(root), *args]
    env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": ""}
    return subprocess.check_output(command, stderr=subprocess.DEVNULL, env=env)


def repository(tmp_path, files=None, *, sha256=False):
    root = tmp_path / "repository with spaces"
    root.mkdir()
    git(root, "init", "-q", *(["--object-format=sha256"] if sha256 else []))
    files = files or {"src/main.py": "print('committed')\n", "README.md": "Project instructions\n"}
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    git(root, "add", "-f", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    return root


def snapshot(root, paths=None):
    return project.snapshot({"root": str(root), "paths": paths or ["."]})


def read(value, path, offset=0, size=project.MAX_CHARS):
    return project.call(value, "read_project_file", {"path": path, "offset": offset, "max_chars": size})


def test_fixed_commit_ignores_dirty_files_and_later_head(tmp_path):
    root = repository(tmp_path)
    spec, value = snapshot(root)
    original = value["commit"]
    (root / "src/main.py").write_text("uncommitted private data")
    assert read(value, "src/main.py")["text"] == "print('committed')\n"
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "later")
    assert git(root, "rev-parse", "HEAD").decode().strip() != original
    assert spec["commit"] == original and read(value, "src/main.py")["commit"] == original
    assert read(value, "src/main.py")["text"] == "print('committed')\n"


def test_only_explicit_prefix_with_component_boundary_is_granted(tmp_path, monkeypatch):
    root = repository(tmp_path, {"src/a.py": "approved", "src-other/a.py": "outside", "README.md": "outside"})
    _, value = snapshot(root, ["src"])
    assert list(value["files"]) == ["src/a.py"]
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("No Git call for denied file"))
    with pytest.raises(project.ProjectError, match="project_file_unavailable"):
        read(value, "src-other/a.py")
    result = project.call(value, "list_project_files", {"prefix": "src-other", "offset": 0})
    assert result["files"] == [] and result["total"] == 0


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", "src/../outside", "src//a.py", "./src/a.py",
                                "src\\a.py", "", ".", "src/\x00", "src/\nname", "a" * 1025, "\ud800"])
def test_invalid_model_paths_never_reach_git(tmp_path, monkeypatch, path):
    root = repository(tmp_path)
    _, value = snapshot(root)
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Denied path reached Git"))
    with pytest.raises(project.ProjectError):
        read(value, path)


def test_secret_paths_are_closed_even_when_committed_and_granted(tmp_path, monkeypatch):
    names = [".env", ".env.production", ".envrc", "src/.env", "src/.ENV.dev", ".ssh/id_rsa",
             ".aws/credentials", ".codex/config.toml", ".claude/settings.json", "config/server.key",
             "config/cert.PEM", "src/secrets.json", "credentials.json", ".git-credentials",
             ".symphony/state.json", ".ours-state/current.json", "tmp_workspaces/job/private.txt"]
    root = repository(tmp_path, {"src/main.py": "approved", "AGENTS.md": "readable rules",
                                 **{name: "SHOULD_NOT_BE_SHARED" for name in names}})
    _, value = snapshot(root)
    assert set(value["files"]) == {"src/main.py", "AGENTS.md"}
    assert read(value, "AGENTS.md")["text"] == "readable rules"
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Secret path reached Git"))
    for name in names:
        with pytest.raises(project.ProjectError, match="project_file_unavailable"):
            read(value, name)


def test_links_gitlinks_and_untracked_files_are_not_exposed(tmp_path):
    root = repository(tmp_path)
    outside = tmp_path / "outside-private.txt"
    outside.write_text("HOST_SECRET")
    (root / "link.py").symlink_to(outside)
    git(root, "add", "link.py")
    commit = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{commit},submodule")
    git(root, "commit", "-q", "-m", "links")
    (root / "untracked.py").write_text("UNTRACKED_SECRET")
    (root / "src/main.py").unlink()
    (root / "src/main.py").symlink_to(outside)
    _, value = snapshot(root)
    assert set(value["files"]) == {"README.md", "src/main.py"}
    assert read(value, "src/main.py")["text"] == "print('committed')\n"
    assert "HOST_SECRET" not in json.dumps(value)


def test_sha256_repository_content_identity(tmp_path):
    root = repository(tmp_path, sha256=True)
    spec, value = snapshot(root)
    assert len(spec["commit"]) == 64
    assert read(value, "src/main.py")["text"] == "print('committed')\n"


def test_replacement_refs_do_not_change_granted_content(tmp_path):
    root = repository(tmp_path, {"src/main.py": "approved", ".env": "PRIVATE_VALUE"})
    _, value = snapshot(root)
    replacement = git(root, "rev-parse", "HEAD:.env").decode().strip()
    git(root, "replace", value["files"]["src/main.py"]["oid"], replacement)
    assert read(value, "src/main.py")["text"] == "approved"


def test_missing_blob_does_not_fetch_or_run_remote_helper(tmp_path):
    root = repository(tmp_path)
    _, value = snapshot(root)
    oid = value["files"]["src/main.py"]["oid"]
    marker = tmp_path / "remote-helper-ran"
    helper = tmp_path / "remote-helper"
    helper.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 1\n")
    helper.chmod(0o700)
    git(root, "config", "remote.origin.url", "ext::" + str(helper))
    git(root, "config", "remote.origin.promisor", "true")
    git(root, "config", "extensions.partialClone", "origin")
    git(root, "config", "protocol.ext.allow", "always")
    (root / ".git/objects" / oid[:2] / oid[2:]).unlink()
    with pytest.raises(project.ProjectError):
        read(value, "src/main.py")
    assert not marker.exists()


def test_git_environment_cannot_inherit_provider_keys_or_repository_overrides(tmp_path, monkeypatch):
    root = repository(tmp_path)
    _, value = snapshot(root)
    for key in ["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GIT_DIR", "GIT_CONFIG_COUNT",
                "GIT_TRACE", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK"]:
        monkeypatch.setenv(key, "PRIVATE_OVERRIDE")
    original = project.subprocess.Popen
    seen = []

    def launch(*args, **kwargs):
        seen.append(kwargs["env"])
        return original(*args, **kwargs)

    monkeypatch.setattr(project.subprocess, "Popen", launch)
    assert read(value, "src/main.py")["text"] == "print('committed')\n"
    assert seen and all("PRIVATE_OVERRIDE" not in env.values() for env in seen)
    assert all(env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_NO_LAZY_FETCH"] == "1" for env in seen)


@pytest.mark.parametrize("group_error", [None, PermissionError, ProcessLookupError])
def test_git_output_and_time_are_bounded_and_child_is_reaped(tmp_path, monkeypatch, group_error):
    fake = tmp_path / "git-fixture"
    fake.write_text(f"#!{sys.executable}\nimport sys,time\nsys.stdout.buffer.write(b'x'*100000)\n"
                    "sys.stdout.flush()\ntime.sleep(30)\n")
    fake.chmod(0o700)
    monkeypatch.setattr(project, "GIT", str(fake))
    original = project.subprocess.Popen
    spawned = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(project.subprocess, "Popen", launch)
    if group_error is not None:
        def deny_group(*args):
            raise group_error()
        monkeypatch.setattr(project.os, "killpg", deny_group)
    with pytest.raises(project.ProjectError, match="project_output_limit"):
        project._git(str(tmp_path), ["cat-file", "blob", "0" * 40], 20)
    assert spawned[-1].poll() is not None
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    monkeypatch.setattr(project, "GIT_TIMEOUT", .05)
    with pytest.raises(project.ProjectError):
        project._git(str(tmp_path), ["cat-file", "blob", "0" * 40], 20)
    assert spawned[-1].poll() is not None


@pytest.mark.parametrize("changes", [{"paths": []}, {"paths": ["/etc"]}, {"paths": ["../outside"]},
                                  {"paths": [".git"]}, {"paths": [".env"]}, {"root": "relative"},
                                  {"commit": "HEAD~1"}, {"commit": "--all"}, {"commit": "HEAD;evil"},
                                  {"root": None}, {"paths": [None]}, {"unknown": True}])
def test_invalid_grants_are_rejected_without_git(tmp_path, monkeypatch, changes):
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Invalid grant reached Git"))
    with pytest.raises(project.ProjectError):
        project.snapshot({"root": str(tmp_path), "paths": ["src"], **changes})


def test_unicode_chunks_empty_files_binary_files_and_large_files(tmp_path):
    text = "код 🛰️ αβ\n" * 1600
    root = repository(tmp_path, {"src/unicode.py": text, "empty.txt": "", "binary.dat": b"a\x00b",
                                 "invalid.dat": b"\xff", "large.txt": b"x" * (project.MAX_FILE_BYTES + 1)})
    _, value = snapshot(root)
    assert "large.txt" not in value["files"]
    parts, offset = [], 0
    while True:
        result = read(value, "src/unicode.py", offset)
        assert len(json.dumps(result, ensure_ascii=False).encode()) <= project.MAX_REPLY_BYTES
        parts.append(result["text"])
        if result["eof"]:
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    assert "".join(parts) == text and read(value, "empty.txt")["eof"]
    for path in ["binary.dat", "invalid.dat"]:
        with pytest.raises(project.ProjectError, match="project_file_not_text"):
            read(value, path)


def test_listing_paginates_without_sending_contents_or_host_root(tmp_path):
    root = repository(tmp_path, {f"src/file_{i:03}.py": "CONTENT_NOT_LISTED" for i in range(251)})
    _, value = snapshot(root)
    names, offset = [], 0
    while True:
        result = project.call(value, "list_project_files", {"prefix": "src", "offset": offset})
        encoded = json.dumps(result, ensure_ascii=False)
        assert len(result["files"]) <= 100 and len(encoded.encode()) <= project.MAX_REPLY_BYTES
        assert "CONTENT_NOT_LISTED" not in encoded and str(root) not in encoded
        names.extend(item["path"] for item in result["files"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert names == sorted(value["files"]) and len(names) == 251


@pytest.mark.parametrize("args", [{"path": "src/main.py", "offset": True, "max_chars": 1},
                                {"path": "src/main.py", "offset": -1, "max_chars": 1},
                                {"path": "src/main.py", "offset": 0, "max_chars": 0},
                                {"path": "src/main.py", "offset": 0, "max_chars": 4097},
                                {"path": "src/main.py", "offset": 0, "max_chars": 1, "oid": "0" * 40}])
def test_invalid_queries_cannot_reach_git(tmp_path, monkeypatch, args):
    root = repository(tmp_path)
    _, value = snapshot(root)
    monkeypatch.setattr(project, "_git", lambda *a: pytest.fail("Invalid query reached Git"))
    with pytest.raises(project.ProjectError):
        project.call(value, "read_project_file", args)


def test_manifest_corruption_scope_and_identity_are_rejected(tmp_path, monkeypatch):
    root = repository(tmp_path)
    spec, value = snapshot(root)
    bad = copy.deepcopy(value)
    bad["files"]["outside.py"] = bad["files"]["src/main.py"]
    with pytest.raises(project.ProjectError, match="invalid_project_state"):
        project.validate_snapshot(bad, spec)
    narrowed = dict(spec, paths=["src"])
    with pytest.raises(project.ProjectError, match="invalid_project_state"):
        project.validate_snapshot(value, narrowed)
    monkeypatch.setattr(project, "_git", lambda *a: b"WRONG_CONTENT")
    with pytest.raises(project.ProjectError, match="project_content_changed"):
        read(value, "src/main.py")


def test_scope_and_metadata_limits_are_enforced(tmp_path, monkeypatch):
    root = repository(tmp_path)
    with pytest.raises(project.ProjectError, match="no_project_files"):
        snapshot(root, ["missing"])
    monkeypatch.setattr(project, "MAX_FILES", 1)
    with pytest.raises(project.ProjectError, match="project_file_limit"):
        snapshot(root)
    monkeypatch.setattr(project, "MAX_TREE_BYTES", 8)
    with pytest.raises(project.ProjectError, match="project_output_limit"):
        snapshot(root)
