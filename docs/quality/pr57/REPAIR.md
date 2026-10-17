# PR 57 factory repair

Scope: the beginner factory CLI, its delivery and recovery gates, and compatibility
with the advanced workflow. The owner approved a score strictly above 90% across
CLI experience, correctness, trust, and engineering, with no failed safety floor.
PR merge and release are excluded.

## Source identities

- Reviewed PR: `5358ac56e1503f4c3ec3ba9e11a484943070a98c`.
- Initial target: `a6c94beed2f91756061e47aa5c700cd3d3d82640`.
- Updated target: `4f17d032d72dec2e9c00fddeec8ab6b09227bf39`, which merged main
  while this repair was active. Its PM work is preserved, not part of this audit.
- Local integration checkpoint: `74708567e097fca1ff8d6314de51184744b43ae6`.
- Sync repair checkpoint: `50574ad934d2cec8a9410782882b44d4bd07b1be`.
- Windows compatibility checkpoint: `d9905030064d285c1f0dfb0a578ad9f543d28886`.
- Failed-commit retention checkpoint: `005e66f43860b093d82f3cb3c3482792295767e6`.
- Browser applicability checkpoint: `01e449c54aea8622e703c63a7d8035048ee0ffa1`.
- Operator diagnostic checkpoint: `3d44b3b5da176763e1da589f73caac33dd10991a`.
- Token-cancellation checkpoint: `dcf9db12b631d8794bdee3b2395822702f9901ef`.

Final validation and scoring are recorded in the quality ledger; historical test
counts in the original PR are not current evidence.

## Delivery and proof

The factory's workflow defines Ready as its dependency wait state, Build as the
implementation stage, Verify as a fresh verification turn, Done as successful
delivery, and Blocked as missing authority or environment. See
`src/symphony/factory/templates/WORKFLOW.md` and its stage prompts.

Previously, the runtime checked only the producing stage's ledger. A complete
Build ledger could therefore accompany a move directly to Done. Terminal
reconciliation could apply the same incomplete rule. The repaired shared gate
also checks the destination and requires fresh Verify completion before delivery.

```python
# Before: the destination did not determine whether Verify was mandatory.
evaluate_contract(producing_state, body, "TASK-1", profile="factory")

# After: normal transitions and terminal reconciliation share destination checks.
evaluate_contract(producing_state, body, "TASK-1", profile="factory", advanced_state=destination)
```

The first form validates what Build wrote. The second additionally rejects a
successful destination that skips Verify. For the reproduced Build-to-Done case,
all five Build headings remain insufficient; the ticket returns to Build and the
dependent ticket stays Ready. Counterexample: Build-to-Verify with the required
ledger is valid, and an explicit Blocker is evaluated separately from success.

Verify commands must match the declarations captured before worker execution.
One criterion can cite multiple commands, and every declared command needs a row
reference. A fresh Verify turn must replace old evidence; browser work requires
a nonempty, fresh report inside the worktree and explicit counts.

Execution output and browser counts remain worker attestations. These gates
prevent known workflow bypasses; they are not cryptographic execution provenance.

## Windows cleanup incident

On 2026-10-17 KST, an automated installed-wheel test completed its first ticket,
ran its product tests independently, and merged the product locally. Cleanup then
emptied the disposable host board, deleting TASK-1 and TASK-2. No owner board was
used. The receipt's UTC sequence is 2026-10-16 21:07:55 (before_remove) followed
by 21:07:56 (stale claims pruned).

Affected artifact: version 0.25.0, SHA-256
`7d46989606385b1525031977c2b2e8d2248a1568669203ee391a900ff227d584`.
Receipt: `.sdlc/work/pr57-factory-quality/logs/qa-wheel-r1/happy-failure/`.

The workspace shared its host board through a Windows directory junction.
Git-for-Windows traversed that junction during forced worktree removal.

```bash
# Before: Git received the workspace with its shared directory still attached.
git -C "$SYMPHONY_WORKFLOW_DIR" worktree remove --force "$PWD"
```

```python
# After: the cleanup script validates ownership, then removes link entries only.
if board.is_junction():
    board.rmdir()
else:
    board.unlink()
```

`rmdir` removes the junction entry; `unlink` removes a POSIX symlink. Neither
recursively deletes its target. The script verifies the workspace is a direct
child of the configured root, shares the host's Git repository, and links to
the actual host board. It also detaches other nested junctions before Git runs.

For the real test row TASK-1=Done / TASK-2=Ready, the old cleanup erased both
cards. The repaired installed-wheel journey preserved both until they reached
Done, then confirmed both products were merged and their real tests passed.
Counterexamples: a real kanban directory, a foreign link target, the workspace
root itself, and a different Git repository are refused before Git removal.

## Recovery and compatibility

- Factory Blocked does not spawn default-workflow repair tickets. The operator
  fixes the named environment or authority problem before retrying.
- Remote publishing is opt-in. Local delivery does not require an upstream.
- `project create` keeps the current advanced template; `factory init` owns the
  beginner template. All eight supported backend configurations are validated.
- Existing default, deep, PM, and release gates are retained from the target.
- Bundled SuperQA adaptations retain their upstream source pins and are listed
  in its manifest and notices. Vendor lint/type checks remain enabled.

## Sync review

Independent review reproduced two additional defects through real file trackers:
simultaneous syncs created two board IDs for one Wayfinder ID, and failed sync
rollback replaced a newer Build state with an old Ready snapshot. A further
review found that preserving an edited card could delete its dependency during
rollback. The factory now serializes imports per board, compares the exact bytes
it wrote under each ticket lock, and preserves the transitive dependencies of
retained cards. Unrelated, unchanged new cards can still be rolled back.

Independent round 3 passed all 25 sync tests and repeated the actual failure:
TASK-2 kept its operator note and TASK-1 dependency after the simulated disk-full
error. Retrying kept both IDs and created only TASK-3. No source-review blockers
remain. Full-suite and installed-artifact checks are separate receipts.

## Real Codex startup on Windows

Codex's workspace sandbox must receive an array of permitted host directories:
the shared board and the worktree's Git administration paths. On 2026-10-17 KST,
the real Codex 0.162.1 smoke on checkpoint `50574ad` exited before its first
turn. The adapter was unchanged from dev `4f17d032`. Its exact injected command
failed with `expected a sequence` for `sandbox_workspace_write.writable_roots`;
plain `codex app-server` initialized successfully in the same workspace and
environment. Authentication was not the startup cause.

```python
# Before: backslashes crossed the Git Bash/native Windows argument boundary.
roots_toml = json.dumps(list(writable_roots), separators=(",", ":"))

# After: normalize only Windows command arguments, keeping the same paths.
command_roots = (
    [root.replace("\\", "/") for root in writable_roots]
    if os.name == "nt" else list(writable_roots)
)
roots_toml = json.dumps(command_roots, separators=(",", ":"))
```

The first form serialized native backslashes that were changed across the shell
boundary before Codex parsed its TOML setting. The replacement first converts
Windows separators, then serializes an array. Native environment values remain
unchanged. For the actual test root (Windows profile name redacted)
`C:\Users\<profile>\AppData\Local\Temp\symphony-factory-qa-r4w56zqf\live-codex\.git`,
the command now passes the same directory as
`C:/Users/<profile>/AppData/Local/Temp/symphony-factory-qa-r4w56zqf/live-codex/.git`.
Counterexample: POSIX paths, including literal backslashes, are not normalized.

The scanner also recognizes Windows directory junctions at the existing
top-level symlink boundary. Its final probe grants exactly the host `kanban`,
common `.git`, and `.git/worktrees/TASK-1` directories; sandbox mode is unchanged.
Real initialization and read-only model discovery now succeed, including
`gpt-6.1-sol` with high reasoning. Full model delivery is a separate receipt.

## Failed Git checkpoint and recovery

Delivery normally records the worker's product in Git before merging and removing
the worktree. On 2026-10-17 KST, real Codex Build and independent Verify executed
the declared two-test product proof in a disposable Windows project. At
2026-10-16 22:24:19 UTC, host Git could not open `product_task_1.py` and returned
exit 42 from the commit wrapper. At 22:24:21, Symphony nevertheless reported
`auto_merge_nothing_to_apply`; cleanup followed at 22:24:22. The two undelivered
product files were lost. The host branch contained only the fixture configuration,
not a product commit. No owner project was used.

The earlier runtime treated a failed local snapshot like a harmless empty commit:

```python
# Before: ignore the snapshot result, then continue toward delivery/cleanup.
await commit_workspace_on_done(workspace_path, ...)

# After: preserve the worktree when the result is not durable.
snapshot = await commit_workspace_on_done(workspace_path, ...)
if not snapshot.durable:
    await orch._preserve_history_failure(cfg, issue, workspace_path, snapshot)
    return
```

The assignment retains the existing Git result. The condition distinguishes an
actual durable commit or valid no-change result from failure. The failure handler
persists an operator pause and a Blocker, stopping merge and removal. For the
actual TASK-1 permission-denied result, `durable` is false and product files stay
in their worktree. Counterexample: a readable, clean worktree with nothing new to
commit may still complete normally. Startup also checks worktree contents before
removing a branch already merged into the target; branch ancestry alone cannot
prove that untracked product files were delivered.

The failed root used Python's private temporary-directory ACL. Read-only probes
confirmed that this differs from an ordinary Windows project with inherited user
permissions. The exact sandbox-created child owner is unknown; the original
files were deleted and the remaining cache is inaccessible. The next fresh
fixture uses normal directory creation, without changing sandbox permissions or
any existing project ACL. Git failure must retain work regardless of this cause.

The operator fixes the named Git problem, returns the card to Verify, and resumes
the paused ticket through the TUI or supported API. These are separate actions.
The installed-package regression uses an owned Git index lock to prove actual
failed-write retention and recovery without reproducing an opaque ACL failure.

## Browser-proof applicability incident

The factory requires browser evidence for routed browser QA, while ordinary
Python tasks use their declared command proof. On 2026-10-17 KST, the R6 real
Codex verifier passed the two declared tests and wrote "No browser surface or
attached `superqa` applies." At 2026-10-16 22:57:31 UTC, Symphony rejected Done
because the old applicability detector searched all prose for the word browser.
The host successfully committed the two product files as `e751495` and retained
the worktree, but local delivery did not finish.

```python
# Before: even a negative sentence made browser proof mandatory.
browser = "superqa" in issue.skills or bool(
    re.search(r"\bbrowser\b", issue.description or "", re.IGNORECASE)
)

# After: explicit applicability plus the current attempt's original requirement.
browser = factory_browser_required(
    issue.description or "", issue.skills
) or bool(authority_entry and authority_entry.factory_browser_required)
```

The helper recognizes an attached `superqa` skill or a Browser Verification H2
section. The second expression preserves a requirement recorded before the worker
ran, so removing positive metadata during that attempt cannot bypass proof.
For the real TASK-1 row, skills were only `supergoal`, no Browser Verification
heading existed, and the negative sentence now leaves browser proof optional.
Counterexample: Wayfinder `browser: true` or `kind: ui` attaches `superqa`, so an
otherwise passing Python table cannot replace the required browser report.

The requirement pin lasts for the running attempt. A new attempt or restart
derives applicability from the current persisted ticket. This is not a new
durable authority or attestation system. Existing report containment, freshness,
and positive-pass/zero-failure checks remain unchanged.

## CLI and manual repair

Five simulated first-use personas exercised the installed CLI. Their findings
led to explicit argument descriptions, short backend usage lines, a hand-written
ticket directory hint, accurate `--force` customization-loss warnings, and a
Windows port diagnostic. Actual screen-reader testing remains unavailable;
plain-text, no-color and terminal-width checks are proxies, not an NVDA pass.

The complete Linux suite found a missing Korean factory caption in generated
manual tables. The generator and both cheatsheets were updated; the original
failure and the final full-suite rerun remain separate evidence.

## Operator endpoint and stage diagnostics

On 2026-10-17 KST, fresh simulated operator sessions used explicit factory ports
52363, 52364 and 52365. The listeners and startup output used those ports, while
`WORKFLOW-PROGRESS.md` still linked to the workflow's default 9999. Progress
rendering normally reads the workflow configuration; the CLI override affected
the listener without reaching that renderer. Both paths came unchanged from
target `4f17d032`.

```python
# Before: progress rendering sees only the persisted workflow's port.
cfg = self._state.current()

# After: use the listener port in a render-only configuration view.
cfg = self._state.current()
if self._server_port is not None:
    cfg = replace(cfg, server=replace(cfg.server, port=self._server_port))
```

The CLI supplies the requested port before observer registration, then the actual
bound port after startup. The render-only replacement keeps the workflow and
reload state unchanged. For the observed configuration 9999 / listener 52363,
the progress link now uses 52363. Counterexample: an explicit
`SYMPHONY_BOARD_URL` still wins, and an explicitly empty value still disables
the link. An ephemeral port uses its actual assigned value.

The same sessions saw `merge_timing: after Document, before Done` in the operator
API, although factory has no Document stage. Factory now reports `after Verify`;
other profiles retain their previous text. This changes the explanation, not
delivery ordering or authority.

The first three persona trials also found a test-provider setup error. Windows
command resolution selected the supplied `.cmd` wrapper, but Git Bash selected
the real extensionless npm executable. Those incomplete sessions were stopped
and retained. The corrected fixture supplies both wrapper forms; initialize-only
probes through the installed backend confirmed all five return the deterministic
fixture identity. Fresh sessions use new empty projects. This fixture correction
does not change Symphony's backend resolution or model defaults.

## Token cancellation after a worker writes Done

The worker writes its verification evidence and requested card state before its
process reports successful completion. The host then records and delivers the
product. On 2026-10-17 KST, R8 TASK-1 had passed two tests and written Done, but at
2026-10-16 23:47:47 UTC its Verify usage reached 664618 against a 600000 cap.
Symphony cancelled the worker. The general terminal-cancellation branch released
the task without merging, but left the card and progress at Done. Host HEAD
remained `5d0706c`; the product and test files were still uncommitted in its
registered worktree. The complete delivery driver correctly failed.

```python
# Before: a token-cancelled Done card follows the ordinary cancellation path.
if terminal_cancelled:
    # Release the slot; do not merge or run Done hooks.
    ...

# After: preserve cancellation safety and explicitly hold undelivered factory work.
if (terminal_cancelled and entry.hit_token_budget
        and cfg is not None and orch._is_factory_profile(cfg)
        and _is_successful_terminal_state(entry.issue.state)):
    await _stop_factory_budget_cancelled_delivery(orch, cfg, issue_id, entry, debug)
elif terminal_cancelled:
    # Existing intentional terminal cancellation behavior.
    ...
```

The first conditions select an actual token-budget cancellation in the factory.
The final predicate recognizes the freshly read success state, including the
repository's Completed alias, so an operator's
Archive, Cancelled or Blocked choice is not replaced. The new handler persists
a pause, moves to a safe configured state (normally Blocked), and adds a Blocker
with the phase usage, retained workspace and recovery instructions. It performs
no commit, merge or cleanup. The operator addresses the budget cause, returns
the card to Verify and explicitly resumes it; a fresh verifier must finish.

For the observed TASK-1 row, `664618 > 600000` now takes the non-success hold path
instead of leaving an undelivered Done claim. Counterexample: an operator-archived
worker without a token overrun keeps the existing terminal cancellation behavior.
Product budget defaults are unchanged; the state-specific Verify limit overrides
the `max_total_tokens` fallback.

## Evidence interpretation

Installed-package fixtures start real Symphony services and worker subprocesses,
execute actual product tests, and inspect Git and board state. The worker's
choices are deterministic; this evidence does not claim autonomous model quality.
Live-model proof, when available, is recorded separately.

Windows PM fixture failures reproduced on untouched target `4f17d032` are
inherited limitations, not factory passes. Their exact results and Linux
CI-parity checks are reported separately in the final verification record.
