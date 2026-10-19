# PM Workspace operations

PM state is stored in the existing `.symphony/state.db` schema v9 and in the selected planning worktree. Keep the worktree and database together when recovering an interrupted operation.

## Recovery rules

- A missing or moved planning worktree sets the claim to `REPAIR_REQUIRED`. Rebind it through the repair action; do not edit the database by hand.
- `EXTERNAL_EDIT` blocks browser and assistant writes. End the mode to re-index. Invalid content remains repair-required.
- A staged handoff is not runnable. Reconciliation publishes only when every ticket still matches the durable approved title, body, request, backend, dependencies, provenance, and content hash.
- Missing, malformed, or removed `symphony_pm` metadata stays managed and non-runnable when a durable mapping exists. It never falls back to an ordinary ticket.
- Revocation and actual process launch share a cross-process per-change lock. An expired, superseded, revoked, or drifted permit cannot start another process generation.
- A `requires_code` successor may launch only after a human accepts the predecessor's exact task revision with the complete approved procedure receipt set, and that accepted commit is present in the successor's actual Git ancestry.
- Cleanup uncertainty retains the owned PID/handle and blocks a retry until process-group cleanup can be confirmed.
- Manual evidence submission does not stop a native attempt and does not complete a task. Acceptance returns `native_attempt_active` while a lease, launch permit, owned process, or uncertain cleanup remains. Stop the native attempt through the existing control, wait for confirmed cleanup, then repeat the acceptance preview and confirmation.
- `order_only` predecessors still block native ordering until terminal, but they do not require accepted-code ancestry. `requires_code` predecessors retain both human acceptance and Git ancestry checks.

## Verification procedures and manual completion

Native command definitions remain under `pm.verification_procedures`. Manual definitions use `pm.manual_verification_procedures` with immutable instructions, a required report, and optional or required artifacts. A managed task opts into an exact set with `verification_policy.version: 1` in its task YAML. If the whole field is absent, the compatibility policy requires every configured native command and no manual procedure. The human-readable `### Verification` bullets are descriptive and never define authority.

For work completed without a native agent run, choose a server-issued procedure in Code Files:

- **Run verification** executes a native procedure in an isolated checkout at the selected tested commit. Symphony derives its exit code, output hashes, timeout, and result.
- **Submit completion evidence** records a manual reported result with a checked-in report blob at the tested commit and any policy-required immutable artifacts. It never invents a run or zero exit code.

Both paths create inspectable evidence first. The task changes only after the complete approved procedure set is ready at one commit and a human confirms the existing acceptance preview. Report downloads require normal authentication and the same stable project identity.

Coverage is a lazy derived view. Requirements and scenarios show specified, linked, implemented, tested, accepted, and waived independently, with per-task evidence and partial counts. Every required linked task must qualify before implemented, tested, or accepted is fully satisfied. A waiver is a revision- and scope-bound review record; it never satisfies execution, acceptance, ancestry, completion, or archive gates.

## Planning assistance

The seven planning actions run in the dedicated tool-disabled proposal client. The server owns document and section selection, reloads the requested revision, and returns reviewable per-document patches. New REQ, SC, SB, API, DEC, T, and G identities use bounded model placeholders; the host reserves and substitutes permanent IDs before review. Replaying one operation retains its IDs. Cancelling or discarding retires them permanently.

Apply checks the operation digest, selected paths, base hashes, permanent identities, WBS structure, and approved-task cancellation rules. It writes all selected documents under one recovery journal and one revision. An incremental draft may apply while the overall change still has readiness findings; those findings are returned and remain blocking at approval and handoff. If planning process cleanup is uncertain, later assistance remains unavailable until the owned process group is confirmed stopped.

The assistant's existing host developer instructions also ask for OpenSpec 1.14.2-ready drafts: concise requirement descriptions within the [500-character limit](pm-workspace.md#configuration), requirement text and scenarios inside their delta, requirement, and scenario blocks, and other explanation in `proposal.md` or `design.md` only when those documents are allowed targets. Otherwise the proposal names the needed target as blocked instead of proposing an unselected document. It must not truncate, drop, or rewrite existing owner content to fit a limit, and targets are never expanded. The `symphony-pm` schema, owner and project custom schemas, proposal-only review, managed IDs, WBS rules, writing rule, pinned model and effort, strict initialize, isolation, no-tools/no-network execution, and approval controls are unchanged.

## Project and package identity

PM authority uses the registered host project ID throughout HTTP, repository, handoff, dispatch, and verification records. Tracker slugs remain tracker configuration and may repeat across registered projects; a stale request carrying another project ID is refused. Standalone services retain one deterministic local identity.

Release evidence binds the installed distribution RECORD, METADATA, package code, and static assets. A metadata or version byte change invalidates the cached binding and requires fresh source-bound evidence.

## Browser origin and owner claims

PM mutations refuse untrusted browser origins with `403 forbidden_origin`, including behind a reverse proxy. A reverse-proxy operator must declare the actual browser origin in `SYMPHONY_TRUSTED_ORIGINS`; the PM error currently does not name that setting. The disposable demo ignores this setting and accepts only its local addresses. `X-Symphony-PM-Owner` supplies the recorded owner claim (default `local-operator`); it is not authentication or an authorization boundary.

## Accepted baseline identities

Later OpenSpec `MODIFIED` and `REMOVED` deltas may reuse an exact accepted baseline REQ/SC identity only with the same capability, semantic kind, and scenario parent. Supported requirement renames retain the original IDs through the next real archive. `ADDED` blocks still receive new server IDs. Cross-capability, fabricated, retyped, reparented, duplicate, and retired IDs are refused.

## Managed source newline behavior

Symphony writes newly generated managed files with explicit UTF-8/LF bytes on every platform. It preserves UTF-8 BOM and mixed LF/CRLF bytes outside an edited span. A no-op save does not rewrite the file or revision. An all-CRLF managed WBS is currently unsupported by the canonical parser, so external-edit re-index places it in repair-required/read-only state before any application write. Convert it to supported LF or mixed source outside the application, then re-index; Symphony never silently normalizes it.

Workbook import requests carry the original plain filename together with base64 content. Only `.xlsx` is accepted, case-insensitively; path-like names, `.xls`, `.xlsm`, and renamed unsupported media are refused before workbook parsing. The validated filename participates in the preview digest and persisted import snapshot. Every XML member is parsed before workbook loading; malformed XML, DTD/entity declarations, hidden rows outside a deceptive declared dimension, and duplicate task identities fail as `unsafe_workbook` without creating a preview.

Read Me records the export's UTC time and counts leaf tasks by supplied execution state. Execution Snapshot values are informational export-time snapshots and do not authorize state changes. HTTP XLSX export reads native tickets once, off the event loop, for published mappings matching the registered project, saved change and task revision. The native planning reference must match the recorded task, revision, handoff and approval snapshot. Missing, stale, unpublished or mismatched tickets supply neither state nor runtime link. Durable published records remain readable after an approval is superseded or the change is archived; export does not require a current active approval. Read Me stays within 64 rows and two columns: at most 48 supplied states appear separately, with the remaining leaf count shown as Additional supplied statuses; WBS retains each full state.

The six sheets and Metadata format version 1 remain unchanged. WBS appends read-only Display Number and Dependency Summary after its existing 12 columns; Dependencies appends Task Title and Predecessor Title after its existing five. Display numbers follow parent/order hierarchy and never replace permanent IDs. Long derived numbers/summaries are abbreviated within the configured text limit with a pointer to Parent ID or Dependencies; allowed task titles remain intact. Runtime Link is literal relative text on existing typed Traceability rows, with no added references or workbook hyperlinks. Import ignores edits to these derived/runtime fields, accepts narrower legacy version 1 workbooks and retains exact extended column limits of 14 for WBS and seven for Dependencies.

Gray cells distinguish existing Task IDs, derived display/dependency fields, runtime links/snapshots, and export provenance; this is a visual guide, not worksheet protection. New tasks still use `Action=Add` and a `NEW-*` temporary ID. Dropdowns are advisory: blank Action defaults to Keep, blank dependency Kind defaults to requires_code, and Reference Type accepts case-normalized values. Custom Type, Priority, and Agent Override values remain valid. Legacy version 1 workbooks still use the same import preview.

## Downgrade

Turning PM flags off hides mutations but preserves planning files, approvals, handoff mappings, evidence, and dispatch denials. Ordinary tickets retain their existing behavior. Do not delete v9 rows to downgrade.

The PM v9 schema is still additive relative to released v1-v8. Use a fresh v8→v9 disposable fixture for validation. A historical pre-release v9 candidate that lacks current v9 tables is reported as an incomplete registry and fails affected actions closed; Symphony does not silently adopt or repair that candidate.

## Archive

Archive is a separate confirmed operation after completion evidence and conflict checks. OpenSpec 1.14.2 writes `openspec/changes/archive/YYYY-MM-DD-<locator>` (or preserves a locator already carrying a date prefix). The adapter accepts only the exact previewed contained destination. Delivery verification archives only disposable fixtures; archiving this repository or another real project requires separate authorization.

An existing `pmChange=<change-id>` link remains available after archive as read-only history, including its planning documents, WBS, managed tickets, verification evidence, and ArtifactStore links. Archived changes stay out of the active change list, and their original IDs cannot be reclaimed, repaired, edited, approved, handed off, or archived again.

## Installed Codex readiness

PM assistance uses the resolved installed Codex executable without automatic upgrades or downgrades. Readiness and planning start share a five-second local probe deadline followed by confirmed cleanup: successful version output, binary-generated ThreadStartParams/TurnStartParams schemas supporting the exact restricted control values, and strict startup/initialize with all existing disabled features. This probe uses an empty isolated CODEX_HOME, strips API keys, and never reads authentication content or starts a thread, turn, or provider call. Login availability is checked separately on every readiness request.

Successful admission is cached only against the resolved executable's device/inode/size/modification/change timestamps and freshly checked version. Replacement invalidates it; JavaScript launchers are reprobed because their native package can change independently. Compatibility failures report the missing control or failed probe and ask for a compatible installed CLI; planning returns `503 capability_unavailable` without relaxed settings or fallback. GPT-6.1 Sol, medium effort, isolated authenticated runtime, read-only/no-network/tool-disabled execution, output limits and unexpected-request refusal remain enforced. Local readiness proves these controls, not provider entitlement or completion; actual assistance requires its own successful turn evidence. Future CLI versions are checked rather than assumed compatible.

## Accepted findings and current handling

These IDs preserve the original accepted Low/Medium findings (R32, R33, and R35 round two) from the PM Workspace review; that local evidence file was never committed. Current handling reflects the local hardening changes and their focused regressions; it does not establish final installed lifecycle, performance, or quality approval. N1 process-group cleanup changes are already recorded in the changelog.

| Finding | Current behavior or handling |
|---|---|
| R32-02 (Low) | Snapshots hold stable per-change authority while resolving and reading the change; archive uses the same lock. Focused concurrent snapshot/archive checks retain complete active or archived history without the missing-lock-path race. |
| R32-03 (Low) | Read-only `git status` uses `--no-optional-locks` and no longer refreshes the index. Approval's `git add` and commit retain normal locking. |
| R32-04 (Low) | Synchronous PM transactions run on worker threads with owned registry connections. File/authority locks and revision conflicts remain enforced; focused HTTP heartbeat and concurrent-writer checks pass. |
| R32-05 (Low) | Atomic replacement and semantic-stage publication/recovery preserve existing regular-file modes; new files remain 0600. Archive recovery uses the fsynced atomic writer and cleans failed temporary files. |
| N2 | Registry recognition failures are contained by the existing `pm_dispatch_denied` attempt result. Uncertain authority stays non-runnable; repairing the registry restores healthy ordinary-ticket progress. |
| N3 | The default pytest suite now invokes `node --test tests/pm_ui.test.mjs`; missing Node or a failing UI suite fails the check. Hosted execution of this local candidate remains unverified. |
| N4 | The owner header is an audit claim. Its meaning is now documented above; the authentication policy is unchanged. |
| N5 | Unchanged: `guarded()` maps every `ValueError` to HTTP 400 `invalid_body`, which can obscure an internal cause. Response-status changes require separately scoped contract approval. |
| N6 | Manual evidence checks and report downloads run off the event loop with the existing PM operation timeout. Timed-out Git processes are killed and reaped; immutable hashes, project checks, and existing refusal errors remain enforced. |
| N7 | PM project switching uses the localized draft-discard guard. Cancel retains the current selection and draft; confirmed discard follows the existing switch flow. |
| N8 | Removed the unused `PmVerificationService.build_from_receipts` helper. Production receipt and acceptance flows remain covered. |
| R2-M1 (Medium) | On a host without `ps`, if a process leader exits on SIGTERM while a group member ignores it, final group SIGKILL may not run. Cleanup stays uncertain and `stop()` raises. This is an existing limitation relative to main. |
| R2-L1 (Low) | Proxy guidance now covers PM `forbidden_origin` above. The runtime PM error still omits `SYMPHONY_TRUSTED_ORIGINS`. |
| R2-L2 (Low) | Unreleased notes now disclose cross-origin PM refusal and rejected pinned planning settings. This is documentation of existing behavior. |
| R2-L3 (Low) | Unknown `pm.*` keys other than `planning_codex_version`, `planning_model`, and `planning_reasoning_effort` are silently ignored. |
| Assistance review (Low) | Empty optional blocked-instruction and reserved-ID sections are omitted. Proposal diffs and server-owned apply/discard authority are unchanged. |

Native Windows PM locking behavior, a real provider quota-fallback event, a real CLI agent leaving background processes, and ps-less Linux cleanup remain unverified platform/provider observations. Historical Linux/macOS runs do not prove them. Current-source checks and hosted CI status belong to the delivered completion evidence; this limitation list is not a fresh PASS. The [human review checklist](pm-workspace.md#final-human-review) must be performed before owner acceptance.
