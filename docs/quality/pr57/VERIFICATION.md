# PR 57 verification

The owner authorized repair and update of PR 57 against `dev`, with a quality
target strictly above 90%. Merge and release are excluded. This record separates
source correctness, installed-package behavior, real-model behavior, and missing
product-quality evidence.

The installed-package repair checks, full Linux regression and configured real
Codex journey pass. Independent product quality is **67%, FAIL** against the
strictly-above-90% target; the [quality ledger](../../../QUALITY-LEDGER.md) records
scores, remaining evidence gaps and the judge's disclosed read-bound exception.
The demonstrated cancellation blocker is closed, but complete qualification is
not claimed.

## Artifact and installed-package checks

- Runtime and full-regression checkpoint:
  `dcf9db12b631d8794bdee3b2395822702f9901ef`.
- Wheel: `oh_my_symphony-0.27.0-py3-none-any.whl`, SHA-256
  `8097fc58d61aff9b585f8fb23a359164bacfcf595a0e889831390a8f0a6e32a2`.
- Runtime Python member manifest: SHA-256
  `394096bb914054c501fdd33a6ab05c354cf99b56d4e416d6fa0e58c0f4d35902`.

Windows Python 3.13.5: all eight R9 installed-wheel journeys pass on their first
run: two-ticket happy
delivery, rejected Build-to-Done bypass, rejected proof substitution, explicit
Blocked recovery, actual Git index-lock failure with retention and recovery,
missing-browser-report rejection for attached `superqa` and an explicit
Browser Verification heading, and token-cancellation retention and recovery.
Successful Python-only Verify turns include the
exact sentence that broke the R6 live run, without a spurious browser requirement.
The Git failure journey checks uncommitted file hashes, unchanged host HEAD and retained Git
worktree registration before removing only the fixture's marked lock. After
restart, no worker runs until explicit resume; fresh Verify then delivers both
products and removes both worktrees while retaining the host cards.

All eight backend configurations load. Direct zipimport, 167 required factory
assets, installed console version and dependency consistency pass. Seven input
probes cover Korean/emoji preservation, repeated import, a 419-character title,
empty source/title, malformed YAML, and corrupt managed provenance with explicit
restoration and retry. Forty fresh CLI commands replay the five simulated
personas' bounded onboarding observations.

A separate backend process initialized successfully, then exited on its first
turn. The operator API exposed a copyable `port_exit` diagnostic and paused retry
state; the host branch stayed unchanged and the worktree survived. The injected
exit was 23, but the diagnostic reported `rc=None`; that limitation is preserved.
All nine fixture service instances were confirmed stopped. Final readback binds
317 command receipts and matching source/wheel runtime bytes to `dcf9db12`.

A service started on an explicit port override wrote the correct progress link;
the endpoint returned HTTP 200 and reported the
factory merge policy as `after Verify`, `local-only`.

The eighth journey reproduces the real cancellation timing: fresh Verify writes
Done and then reports 664618 tokens against its 600000 ceiling before completing
the turn. Symphony writes Blocked and a copyable Blocker, preserves uncommitted
file hashes and Git registration, and leaves host HEAD unchanged. After a finite
fixture budget adjustment to 700000 and returning to Verify, restart dispatches
no worker until explicit resume. Fresh Verify then delivers both dependency
tickets, the host tests pass, and both worktrees are safely removed.

An owned sync process was terminated after importing five of 40 tickets. All five
survived and parsed. Retry completed the graph with unique provenance and stable
IDs; another retry left all 41 cards, including an operator card, byte-identical.
The operator's existing file and card also survived unchanged. The interruption's
position inside an atomic file operation is unknown; this proves interrupted
batch recovery, not a kill during a specific rename instruction.

The earlier R6 bypass driver read a fixture card during its truncate/write window.
Its failed receipt is preserved; a bounded read retry passed against the same
wheel with the final state and data assertions unchanged. All seven R7 journeys
passed on their first run. The earlier failure remains available as historical
evidence.

## Full regression and source review

On exact checkpoint `dcf9db12` and tree
`11bdf077dd2de7d98f46d81e7a32d4272200dd58`, Linux Python 3.12.3 passed all
CI-equivalent gates: Ruff, translation checks, Pyright, **3520 non-timing tests**
and **10 isolated timing tests**. Combined coverage is **85.65%**, above the
unchanged 80% threshold. The extracted source tree matched the committed tree
before execution and remained clean afterward.

There are 101 existing configured skips: 74 opt-in browser tests, 18 OpenSpec or
nested npm/equivalence cases, three native Win32 cases, three backends outside the
preview contract, and one each for a duplicate hermetic CLI check, a clean-wheel
environment check, and an older Node 20.17 case. These are disclosed rather than
counted as passes. The CI workflow is unchanged from the approved dev target.

Native Windows browser/contract checks passed 454 tests, with one skip and 14
deselections for the documented inherited PM/native fixture cases. Earlier
retention validation passed all eight safety cases, release/workspace checks
passed 106 tests with 36 existing platform skips, and the complete reconcile file
passed 17 tests. These targeted runs are not a full native Windows pass.

The final cancellation repair passed 65 targeted Windows checks. Independent
review ran 42 checks and caught a successful-state alias edge; the shared state
predicate and regression cover both Done and Completed. Real Git worktrees,
file-board updates and persisted SQLite pause flags cover restart/resume and
state/note-write failures. This preserves the original cancellation controls
and adds no default budget increase.

The final diagnostic delta passed 28 focused Windows tests and source Ruff/
Pyright. Its broader related-module run completed with 329 passed, one skipped
and 12 failures in PM authority fixture setup (`approved snapshot input bytes
are incomplete`), before the changed diagnostic methods. No PM checks were
altered. This failed native result remains separate from the complete Linux pass.

Independent source review closed concurrency/rollback defects, Windows startup,
failed-commit cleanup, dirty startup cleanup, and release-handoff cleanup findings.
Its retention safety run passed 16 tests and final browser-applicability review
passed 65 tests. The first Linux run after the snapshot guard
failed two old dummy-workspace mocks; their typed success results were corrected
without changing assertions or production guards, and the entire suite above
was rerun successfully.

## Local performance and reference comparison

With owned services and heavy test jobs stopped, five measurements after one
warmup produced nearest-rank p95 values of **1162.38 ms** for help, **1480.29 ms**
for init and **1196.80 ms** for two-ticket sync. All meet the predeclared
3000/5000/5000 ms budgets. These are small-sample local measurements, not field
percentiles or model-provider latency.

For the same supplied task and endpoint of one dispatchable card, factory setup
took **3.734 s / six commands**, while advanced setup took **4.375 s / three
commands**. The comparison excludes human reading, planning, worker execution
and delivery. It supports a bounded setup comparison only.

## Real Codex on the final wheel

R9 completed on its first configured attempt using authenticated Codex 0.162.1,
`gpt-6.1-sol` / high reasoning. Build and independent Verify ran the declared
product tests. Local fixture merge `2deddad9e295f9ca588f3e4ab67fa5c5d677e9c1`
delivered exactly `product_task_1.py` and `test_task_1.py`. The host rerun passed
both tests; the Done card survived, the worktree was removed, only the host
worktree remained registered, no Git remote existed, and the service stopped.

Configuration was Build 900000 tokens, Verify 600000 (default 350000), fallback
`max_total_tokens` 1250000, and five total turns. This is a successful explicitly
configured integration test, not proof of default-budget or default-model success
for every account. The separate deterministic overrun test qualifies the repaired
cancellation and recovery path without relying on a stochastic token overrun.

## Historical R8 real Codex delivery and budget recovery

The preceding R8 wheel ran authenticated Codex 0.162.1 with explicitly configured
`gpt-6.1-sol` / high reasoning in an owned disposable repository. Build created
the product and ran its tests; a separate Verify session ran the declared proof.
That first attempt exceeded its configured token budget before delivery and is
recorded as **FAIL**, even though the card had already been written as Done.
Its product files remained in the registered worktree and host HEAD was unchanged.

The operator increased only this fixture's finite Verify ceiling from 600000 to
900000 and its `max_total_tokens` fallback from 1250000 to 2000000, moved the card
back to Verify and restarted. The state-specific ceiling takes precedence over
that fallback; the observed cancellation was caused by the Verify ceiling.
The resume API reported `changed: false`; recovery was not dependent on clearing
a persisted pause. A fresh verifier completed, commit `2ba34f0` contained exactly
the product and test files, and local fixture merge
`2342289dd8f1f8865a45f9c8b754cd6107286138` delivered them unchanged. The host-side
test rerun passed both tests, the worktree was removed, the Done card remained,
no Git remote existed, and the owned service stopped. Recovery is **PASS**.

This exposed an operator-status defect: a budget-cancelled verifier could leave
a Done card before actual delivery. R9's new installed regression reproduces the
same usage event and verifies the safe Blocked state, retention, persisted pause
and explicit-resume recovery. The R8 initial failure remains alongside its
separate recovery and is not converted into a clean first-attempt pass.

The recovery recorder initially aliased its nested before-budget dictionary, so
its printed before value followed the later Verify override. The receipt was
corrected from the preserved pre-edit workflow; the original summary and output
remain available. No product execution or other result was altered.

## Fresh simulated operators on the final wheel

Five new fresh-context R9 agents completed their journeys through the installed
CLI and public docs. Every session reran the two declared tests on the delivered
branch, confirmed clean Git state and worktree cleanup, and stopped its service.
No fixture repository had a remote.

| Simulated operator | Journey and local delivery commit |
|---|---|
| New user | Delivery, `fd68aa2d780af08aeca0e3a95360ff1160247ceb` |
| Careful reader | Blocked recovery and delivery, `5451ecaae6f2f69b16f11042f322615f87f5fe86` |
| Hurried user | Delivery, `ec26be15d59a31c3cda9098acdab24cadcb477cd` |
| Plain-output accessibility persona | Delivery, `3dc7dca98c9b243e5ac3dc49cb9fa9f820b5f8e5` |
| Korean-speaking user | Blocked recovery and delivery, `2a3db0a7ac711b97828c1527276ff5562795079e` |

The new user initially omitted the printed ticket-directory prerequisite; the
hurried user guessed `board list` instead of `board ls` and a product filename.
Both recovered using the product's public guidance. Their recorder/shell quoting
mistakes are retained separately from product behavior. Korean detailed recovery
still required the English reference. No-color 80/120-column output carried
explicit states, but long raw lines remained. This is simulated qualitative
evidence with controlled workers, not human testing, NVDA or native wrapping
verification.

## Historical R8 fresh simulated operator sessions

Five fresh-context agents used separate empty projects, the installed R8 CLI,
approved ticket inputs and public product instructions. New-user, hurried and
plain-output accessibility personas completed normal delivery; careful and
Korean-speaking personas completed explicit Blocked recovery. Every session
independently reran the two product tests on its delivery branch, checked Git
history/worktree cleanup and stopped its owned service. All five completed.

Their local merge commits were `4f3073f`, `ee90c23`, `463e9b8`, `e3469ff` and
`4103287`, respectively. These were disposable fixture branches with no remotes,
not PR merges. Raw commands and failed supplemental QA helper attempts were
retained rather than edited out.

Observed friction: a short idle-looking Build interval before the configured
30-second poll, long paths and single-line JSON at narrow widths, historical
Blocker text retained in completed cards, and English CLI/recovery details for
the Korean-speaking operator. The accessibility persona used 80/120 columns and
no-color output; it did not run NVDA. These are simulated qualitative observations
with deterministic local workers, not real-user testing or autonomous model
quality evidence. They remain bound to R8.

## Evidence boundaries

- Installed-package journeys use real managed services, Git repositories,
  worktrees, subprocesses, and product tests. Their worker choices are fixtures.
- Real Codex runs use the authenticated external CLI and its workspace sandbox.
  The first R8 run exceeded the configured 600000 Verify ceiling at 664618 phase
  tokens (1285199 reported cumulative tokens). The card already
  said Done, but local delivery had not occurred. The driver correctly failed;
  uncommitted product files and worktree registration survived. Operator recovery
  is recorded separately with finite 900000 Verify / 2000000 fallback limits. These
  fixtures do not establish that the unchanged 350000 default Verify budget
  suffices for this machine's inherited agent context.
- Five fresh final R9 first-use personas completed delivery or recovery; R8
  sessions remain historical. Separately, CLI replay is surface evidence from
  earlier observations. None is a human usability study.
- Plain text, no-color output, and 80/120-column checks are accessibility proxies.
  An actual NVDA session has not been captured.
- Local latency measurements are five samples after a warmup on this Windows
  machine. They are not field percentiles or external-provider latency.
- Dependency advisory checks and a limited secret-pattern scan do not constitute
  a complete security audit. External model-provider payload traffic was not
  traced.

The final repository pattern check read all 786 committed Git blobs, including
binary and symlink blobs without following links. Its single marker hit was a
69-character malformed PEM-shaped test string with invalid Base64; the hit and
its separate triage were retained without printing its contents. The wheel's 449
members had no matches for the same four patterns. These limited scans do not
establish complete credential, history or external-data safety.

## Compatibility limitations

The complete Linux suite is the CI-equivalent full regression gate. Native Windows
checks cover the changed factory, backend, workspace and orchestrator boundaries.
An earlier broader native Windows run was incomplete and had failures; it is not
reported as a full Windows pass. Representative PM failures reproduce on untouched
`dev` commit `4f17d032`, and native Pyright also reports the inherited PM `os.fchmod`
platform issue. Linux type checking covers the full source. Unrelated PM changes
remain outside this repair.

The PM frontend dependency audit found two low-severity advisories in unchanged
dependency inputs. No moderate, high or critical advisories were reported by that
audit. The unpublished local Symphony package is not covered by the PyPI advisory
database.

## Reproduction

Use the repository's full-strength checks from `CONTRIBUTING.md` and
`.github/workflows/tests.yml`. Do not replace failed checks with reduced thresholds.
The quality ledger records regression guards and final source/artifact identities.
Detailed local receipts remain in the ignored
`.sdlc/work/pr57-factory-quality/logs/` directory, including historical failures.
Only reviewable summaries are committed; local run state and provider logs are not.
