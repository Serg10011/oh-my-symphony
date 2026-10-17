# Factory recovery follow-up

This follows [PR 57](https://github.com/cskwork/oh-my-symphony/pull/57), merged
into `dev` as `6b89e6e08cb24c16b27442187c4a804d1b0f9711` on 2026-10-17 with
the owner's authorization. The first recovery checkpoint was
`d005c616512070c073079f73b5b470d75ea75774`; the setup/long-path repair is
`c68b9224ffddb20a54781c844281eb8e257c6b8b`. No release or promotion to `main`
is part of this work. The target remains strictly above 90% across all four
dimensions, with no failed floors; source review alone does not meet that bar.

## Windows crash recovery

Symphony records worker ownership before dispatch and preserves a workspace
while an earlier worker might still be alive. During installed-wheel testing,
an orchestrator-only crash in Build and Verify exposed a recovery dead end:
the Windows process-group probe always returned unknown, even after the actual
workers had exited. Explicit resume could not establish safe ownership.

The old platform probe remains conservative for legacy records:

```python
if pid <= 0 or sys.platform == "win32":
    return None
```

The condition includes Windows; `None` means unknown, so it cannot authorize
reuse. Replacing that result with a missing PID check would be unsafe: a
grandchild may survive after its parent and grandparent disappear.
See the ownership probes in [`_shell.py`](../../../src/symphony/_shell.py).

New launches instead use a private Windows job and a gated supervisor. The job
contains ordinary descendants before any backend runs. The host must durably
record its exact identity before sending GO; an owner-process monitor remains
active afterward. Reclaim has an additional identity-based path:

```python
if is_job_identity(expected_identity):
    exists = job_exists(expected_identity)
    if exists is False:
        return "not_found"
    if exists is None or not terminate_job(expected_identity):
        return None
```

The first line selects the recorded job identity. The query distinguishes an
empty or absent owned job from an unknown result. Proven absence permits
reclaim; uncertainty or failed termination preserves the hold. A live job is
terminated only through that identity, followed by another absence check.

In the candidate Build preflight, the operator pause was `true` and only service
PID `31124` was killed, without `/T`. The product hash remained
`c59afe37bd0cf751d180214ca37463f3c8ae0e173fe9b0e3fd1572d55fe85b36`
across restart and final delivery. That pause still required explicit resume.
This is historical candidate evidence; final-build qualification is recorded
separately. A legacy record with no provable identity remains a counterexample:
an absent numeric PID does not release it.

Containment covers ordinary process creation, not external brokers, WMI,
services, scheduled tasks, or preexisting daemons. It is not a security sandbox.
No schema or public configuration change was introduced.

## Shared-board preservation

Workers edit the host board through a link in their worktree. Two Windows faults
appeared in the advanced reference workflow during disposable-project testing.

First, its before-run guard compared path spelling:

```bash
# Before
if [ "$(readlink "$target")" != "$source" ]; then
# After
if [ ! "$target" -ef "$source" ]; then
```

The old comparison rejected equivalent Windows and Git Bash path spellings.
The new condition compares filesystem identity. A correct host-board junction
now passes. When the host board exists, a workspace link to a different or
missing target still fails with exit 42; a nonexistent host board is intentionally
skipped by this guard.

Second, the old removal hook called `git worktree remove --force` while the
shared-board junction was attached. Native Git traversed it and deleted the
host card in the owned probe: its prior SHA-256 was
`8381abd8c372abc0e42bae031898753de0f683eef6049549f8685c8454f9f5ce`;
after removal the card was absent. No owner project or data was used.

The shipped [advanced guard](../../../examples/advanced/WORKFLOW.file.example.md)
and [shared cleanup helper](../../../src/symphony/_worktree_cleanup.py) contain
the validation and detachment boundaries below.

The factory and shipped advanced removal hooks call the same packaged cleanup helper. Its
important order is:

```python
# Validate root, exact registration, repository and every board target first.
for board in boards:
    if board.is_junction():
        board.rmdir()
    else:
        board.unlink()
# Detach remaining directory junctions, then invoke Git removal.
```

Validation runs before mutation. `rmdir` removes a junction entry; `unlink`
removes a symlink entry. Neither operation walks the linked board. Git receives
the bounded worktree only after the links are detached. Real directories,
wrong repositories, wrong board targets and denied detachment are refusal
cases, not alternate removal paths. Native Windows and POSIX regression guards
exercise the shipped adapters and preserve target bytes.

Existing copied workflows are not automatically rewritten by a package upgrade.
Review and refresh their cleanup hooks and copied setup script before Windows delivery, following the
[operator guide](../../../skills/symphony-skill/reference/factory.md). The
workspace manager's existing behavior after a custom hook fails is unchanged;
helper refusal is not a general promise that a service will retain a directory.

### Setup refresh and long paths

Qualification of checkpoint `d005c616` found a second removal caller in the
advanced setup script. After automatic repair delivery, reopening the source
ticket reran `after_create`; setup still removed the retained worktree directly:

```bash
git worktree remove --force "$WORKTREE_PATH" 2>/dev/null || true
git worktree prune 2>/dev/null || true
```

The first command could traverse attached directory junctions, and both lines
hid failure. In the isolated advanced recovery run, source setup began at
04:54:26Z and both shared host cards were absent by 04:54:28Z on 2026-10-17.
The copied script matched the frozen source. Its suppressed removal exit is
unknown. The preceding repair-worktree cleanup separately logged Git exit255
with `Filename too long`.

The replacement uses private setup preparation in the shared helper, before
branch mutation. It validates the expected branch, exact worktree registration,
repository and all shared boards, then detaches links before removal. Fresh
empty directories remain available for ordinary `worktree add`. Validated stale
empty registrations use the existing default metadata prune, with checked
exit status and registration readback. That inherited prune can also remove
unrelated unlocked stale registrations; valid and locked records remain. No
forced branch checkout or new expiry override is introduced.
See the [canonical setup script](../../../scripts/symphony-setup-worktree.sh).

On Windows, removal uses `git -c core.longpaths=true worktree remove` for that
command only. Global Git settings and Windows policy remain unchanged. The
native probe reproduced exit255 with a 351-character leaf and successful
removal with the command-local option. A locked registration, another branch
checkout or wrong board target is a counterexample: preparation must refuse
before removal, pruning, branch mutation or add.

The nested-board fixture also exposed a missing parent directory for temporary
tracking metadata; setup now creates that parent before recording the tracked
paths. Product Preview and continuous-improvement baseline still have separate
raw-removal callers outside this repair. Their cleanup deserves a follow-up;
this change does not claim to harden every repository removal path.

## Recovery messages after delivery

A failed Git snapshot or token cancellation leaves work preserved and an active
Blocker explaining recovery. Previously that imperative message remained after
successful delivery. The final delivery gate now changes only a recognized
owned heading:

```text
Before: ## Blocker
After:  ## Resolved Blocker (history)
```

When the recognized note is still present at delivery, the original message remains
byte-for-byte below it. Raw splicing preserves
frontmatter comments, ordering, CRLF and unrelated sections. A resumed attempt,
Done label, local worktree commit or failed remote publication does not resolve
the note; durable target delivery is required.
The [file tracker](../../../src/symphony/trackers/file.py) performs the splice;
the [orchestrator](../../../src/symphony/orchestrator/core.py) invokes it at the
durable delivery boundaries.

Independent review found two parser edges before acceptance: headings inside
multiline Git details split the owned note, and an expanded match could consume
operator additions plus a later note. The accepted matcher stops at the earliest
complete diagnostic and refuses ambiguous boundaries. For a real regression
fixture containing an operator warning after the first diagnostic and a second
owned diagnostic, only the second heading changes. Repeating delivery produces
identical bytes. Operator edits, repeated recovery footers and ambiguous older
unfenced headings remain unchanged conservatively.

The configured real-model recovery delivered the retained product successfully,
but did not establish end-to-end preservation of its original diagnostic. The
739-byte note was absent from the final card, which instead said "Resolved for
this resumed Verify turn" and retained the usage/cap `631270/600000`. There were
no intermediate card snapshots to identify the writer or rewrite timing. The
strict history assertion remains failed; no parser malfunction or worker cause
is inferred from the final state alone. Passing controlled history guards does
not erase that live evidence limit.

## Regression and qualification boundaries

The first checkpoint passed independent Standards and Spec reviews, including 170
targeted tracker tests and 10 independent raw-byte/idempotence probes. The
integrated Windows lifecycle selection passed 571 tests with 19 existing
platform skips. The new Windows CI job runs the native ownership and cleanup
guards alongside the unchanged Linux suite and 80% coverage threshold.

The setup/long-path delta also passed independent Standards and Spec review.
Its integrated native selection passed 609 tests with 22 configured skips.
The additional three skips are POSIX executable-wrapper CAS probes; their
POSIX run passed. The full installed-artifact result is recorded separately.

Hosted Windows CI reported two child exit-1 failures, but did not capture child
stderr or its actual encoding. Local probes reproduced that symptom: the test
child printed non-ASCII JSON through text stdout, and forced cp1252 or ASCII
raised `UnicodeEncodeError` before its requested exit status. The local probes
establish the fixture's encoding defect; the precise hosted encoding remains
unknown. Commit
`ef9fa36703278373272e019118137938f44ffb81` changes only the fixture:

```python
# Before: text stdout selects the environment's encoding.
print(json.dumps(payload, ensure_ascii=False))
# After: emit the UTF-8 bytes required by the test's pipe contract.
sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
```

JSON preserves the Unicode values; explicit encoding determines their bytes;
the binary write avoids an unrelated text-codec conversion. For the real
status-23/cp1252 probe, the old child returned 1 and the repaired child returned
23 with identical intended Unicode data. All six encoding/status combinations
passed, along with 28 containment guards. Every transport, environment,
working-directory, exit-status and job-cleanup assertion remains in place.
The runtime did not change. Both hosted jobs subsequently passed in
[run 39008533692](https://github.com/cskwork/oh-my-symphony/actions/runs/39008533692).

Full native Pyright still reports the unchanged PM `os.fchmod` typing issue in
`src/symphony/pm/documents.py`; it was not hidden or changed in this scope.
NVDA app access timed out twice despite the owner's authorization. No native
screen-reader pass is claimed. The final verification record and independent
quality assessment must distinguish these gaps from passing source checks.
