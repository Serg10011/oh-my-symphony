# Factory recovery verification

**Independent quality: 75%, FAIL; required target strictly above 90%.**
The [complete independent assessment](ASSESSMENT.md) covers all 27 criteria and
ten floors. It identifies evidence gaps, with no confirmed current floor breach;
unverified floors still block the quality gate.

Candidate: `c68b9224ffddb20a54781c844281eb8e257c6b8b`.
Tree: `c0dfdce851bf4d8fedcad1fd761a0542eb4e13a8`.
Windows wheel `oh_my_symphony-0.27.0-py3-none-any.whl`:
SHA-256 `170a3c6c1d9e9094797460b0ca4882b600d19b781d6cdc5e2afd9097e60440fa`.
Two clean-source builds produced identical bytes. Installed runtime: Python3.12.10.

## Current automated evidence

| Check | Result |
|---|---|
| Full Linux non-timing suite | 3,678 passed, 129 configured skips |
| Linux timing suite | 10 passed, 1 configured skip |
| Coverage | 85.1884648307211%, 28,861/33,879 lines; unchanged 80% requirement |
| Linux Ruff, translation and full Pyright gates | Pass |
| Native Windows lifecycle/setup selection | 609 passed, 22 configured skips |
| Independent Standards and Spec review | Pass; no source blockers |
| Installed factory delivery/failure scenarios | 8/8 pass; controlled subprocess workers and real product tests |
| Installed Ready/Build/Verify/Blocked/Done interruption and recovery | 5/5 pass; actual service-only kill, no descendant kill switch |
| Slow/timeout/retry dependency cases | 7/7 pass; every owned service stopped |
| Corrupt-data and Git permission cases | 5/5 pass; exact ACL restored |
| Previous-release upgrade | Pass for paused v0.26.0 data compatibility; copied-hook delivery migration not claimed |
| Factory/advanced full delivery and recovery comparison | Four controlled endpoints pass |
| Fresh simulated operators | Five assigned endpoints pass; limitations and assistance below |
| Independent product-quality judgment | 75%, average 3.75/5, FAIL; Experience 3, Function 4, Consumer 4, Engineering 4 |

Both hosted jobs pass at test-only follow-up `ef9fa36703278373272e019118137938f44ffb81`
([run 39008533692](https://github.com/cskwork/oh-my-symphony/actions/runs/39008533692)).
Only `tests/test_windows_containment.py` differs from the runtime checkpoint:
the fixture writes explicit UTF-8 bytes instead of locale-dependent text stdout.
Runtime, package, shipped hooks and public product instructions are unchanged.
The first hosted Windows failure remains recorded; assertions and skips were
not weakened. Local forced cp1252/ASCII probes reproduced the fixture fault;
the original hosted child encoding was not captured.

## Fresh operator comparison

| Simulated operator / path | Recorded seconds | Recorder invocations | Assistance records |
|---|---:|---:|---:|
| New user / factory delivery | 170.01 | 11 | 0 |
| Careful reader / advanced delivery | 218.81 | 16 | 0 |
| Hurried / factory recovery | 291.39 | 27 | 1 |
| Text accessibility proxy / advanced recovery | 323.31 | 18 | 0 |
| Korean documentation / factory delivery | 454.32 | 22 | 1 |

Every endpoint required actual local merge, two fresh host product tests,
clean Git state, worktree cleanup and stopped service. Build and Verify ran in
different worker sessions. The advanced recovery included an automatic FIX
ticket, its own independent stages and delivery, and reopening the source
through its normal stages with the dependency retained.

These are five separate fresh-context agents with public product documentation
and supplied input, not humans. One sample per profile with different personas
is descriptive, not a randomized or paired speed benchmark. Times include
reading, thinking, mistakes and waits; runtime/bootstrap installation is excluded.
Recorder invocations can contain several commands, and a shell error can occur
inside a zero-exit invocation. Counts therefore do not measure every action or
mistake. The rough elapsed estimates in operator notes do not replace the clocks.

The hurried persona's assistance record reports no coaching and records an
approval-rejected overwrite attempt plus setup/harness errors; it completed
using the already-generated bundle. The Korean persona's original clock includes
a rejected recorder command and accepted same-route approval re-review after
task provenance was checked. That was environment authorization assistance,
not product coaching. Both records remain visible. The trial also found long
paths, compact JSON/YAML, Git branch setup and English-only detailed guidance
to be usability friction; no native accessibility pass follows from completion.

Quiet local CLI measurement, after task-owned services/tests stopped: five
samples after one warmup. Nearest-rank p95 (the maximum of five) was help
1179.19ms, init 1353.89ms, and two-ticket sync 1263.48ms. All meet the declared
3000/5000/5000ms budgets. This is a local lab measurement, not field latency.

Repository Doctor refuses dispatch from the protected source repository, as
designed. Its default port9999 was occupied by a preexisting Node process;
that process was left untouched. Every disposable service project must pass
its own Doctor check before launch. Full native Pyright retains the unrelated
PM `os.fchmod` typing error; full Linux Pyright passes.

## Evidence limits

NVDA app access timed out twice after owner authorization. The task's portable
NVDA processes were stopped. Redirected 80/120-column output and simulated
operators cannot establish real screen-reader, terminal font or theme behavior.

The atomic-writer's before/after-replacement and real Windows failed-replace
receipts are reused by exact installed module, Python and product-dependency
hash equality (`tests_rerun=false` for those three cases). Only the shared
cleanup module and wheel RECORD changed between the two artifacts. Three
bounded 256MiB experiments did not retain a partially written temporary file;
mid-write interruption remains an evidence gap, not a corruption observation.

The previous candidate's real Codex run hit its configured Verify token limit
at 631270/600000, safely paused in Blocked and retained work. Generated Verify
default 350000 was not used in
that configured run. Prior metadata tracing retained encrypted destinations
but zero native request categories; it did not prove every host or payload.

One explicit recovery on this candidate used Verify 900000, total 2000000 and
the unchanged five-turn limit. It delivered commit
`6c427636a6ea4af704544467fb3cb2398c1113b1`, preserved both product/test hashes,
passed the delivered tests and removed the worktree. Its stricter history
assertion failed: the original 739-byte diagnostic was absent, although the
final Blocker explicitly described it as resolved and kept `631270/600000`.
Writer and rewrite timing are unknown. This proves bounded recovery, not a
fresh Build or default-budget model run, and not exact live-history preservation.

The recovery trace retained 79 known native events, including 28 model-request
events, and 57 encrypted connections (56 to chatgpt.com, one redacted unknown).
It dropped 27 unknown records/requests. Direct sockets, proxy-ignoring clients,
brokers and per-host encrypted payload semantics are outside the observations;
complete external-host/privacy agreement remains a gap. No TLS interception or
raw credential/payload capture was used. Exact installed Python dependencies
have no reported advisories; the existing low KaTeX/Mermaid PM asset dependency
finding remains. These are source package dependencies, not `devDependencies`,
and bundled PM assets ship in the wheel. CLI reachability and exploitability
were not tested. The four-pattern secret scan is bounded, not a security audit.

The advanced comparison uses the same controlled worker environment on both
paths: Python bytecode writing disabled and `/log/` excluded locally. This
avoids generated-file noise in the deterministic fixture; it is not a claim
about the unmodified default environment. Bootstrap uses a clean source
checkout for existing `project create`; runtime services use the pinned wheel.

Two private qualification failures remain preserved. The r3 advanced worker
incorrectly merged the target during Document, contrary to the shipped stage
contract; the actual host conflict gate stopped it, while board/setup/cleanup
remained safe. The corrected worker performs no merge, documents the same
already-proved total topic, and retains the real merge-tree/final merge checks.
All four r4 endpoints pass. This does not qualify arbitrary concurrent wiki
conflict resolution. Separately, an old-release fixture input disappeared before
the r2 upgrade test invoked the old CLI. Its cause is unknown. Seventeen required
inputs were reconstructed from verified Git blobs of v0.26.0 commit
`2f004f1cd7aef8a2ecd051ebe8fe12d276c0ea4a`; the unchanged old wheel/runtime and new
candidate then passed the r3 paused-data upgrade check.

All raw logs, controlled providers and disposable projects remain private.
Public summaries must not imply human usability testing, default-budget model
success, a provider SLA, exhaustive security coverage or a release.
