# Repository instructions

## Scope and execution

- Complete authorized local work through implementation, relevant verification, and documentation. Reuse session decisions and approvals; a phase boundary or optional tool setup does not require reconfirmation. Pause only the part that depends on missing information or authorization.
- Apply `karpathy-guidelines` to substantive code changes; load it once per task. Use available CodeGraph tools for structural questions and `rg`/direct reads as fallback. Do not block on initializing an index or request optional setup merely because it is absent.
- Market Maker is V2-only: `run_volume_market_maker.py`, `core/services/market_maker_v2/`, `config/market_maker_v2/`, the two V2 analysis scripts, and related tests/docs. V2 owns order safety in `order_manager.py` and `execution_models.py`; do not restore/import V1 or its Gate/toxicity/campaign pipeline.
- Do not modify Grid-specific strategy/runtime code, configs, or tests unless a demonstrated fatal shared-component conflict makes it unavoidable. Shared Lighter adapter changes needed for MM are permitted within scope: preserve Grid defaults, explain actual impact, and verify relevant regressions.
- Product authority is `docs/CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md` and `docs/mm_v2/`, subject to later user instructions. Current local sequence is plan §19.7: short dry validation, then preparation of a concrete minimal canary. Historical "not authorized" notes are dated status, not a veto of later authorization.

## Authorization and trading invariants

- Default to read-only and `dry_run: true`. Each live run requires explicit authorization for its network/account/symbol, duration, size/exposure, loss limits, and bounded exit before connecting or mutating. Approximate account balance is not allocated capital or loss authorization.
- One run authorization covers normal quotes, cancellations, and bounded exit within those limits; do not ask again for every order or stop. Per-order "authorization" in code means fresh programmatic account/risk checks, not another human approval. New runs, increased limits, margin/leverage changes, and operations on another host require authorization covering that action. VPS remains deferred under the user's current instruction.
- Live startup requires `--authorize-bounded-flatten`; stop/deadline must attempt the already-authorized bounded exit. Verify network, identity, symbol exclusivity, fees, open orders, position, and risk limits before live work. After stopping, verify process exit and fresh authenticated position/open orders `0/0`; report residual or uncertain state honestly.
- Normal maker quotes are `POST_ONLY`; no self-trading or wash-volume flows. Fail closed for new risk on stale/untrusted data, unknown orders, uncertain mutations, reconciliation failure, or monitor failure. Preserve the existing known-order cleanup and bounded-exit paths; do not let an agent-workflow pause interrupt authorized cleanup. Unknown execution state must reconcile before further mutations.
- Keep financial YAML values as strings and financial arithmetic as `Decimal`. Preserve exact accounting, inventory/loss limits, and order ownership/terminal proof.
- Never print or commit credentials, tokens, wallet profiles, signer objects, `.env*`, logs, secret-bearing exchange configs, or local live/test YAML. Sanitized diagnostics and sanitized example configs are permitted.

## Proportionate verification

- Use the repository `.venv` and `unittest`. For ordinary code changes, run affected public-contract and relevant execution/replay tests. Documentation/instruction-only changes do not require product regression suites.
- For a completed runtime change batch before handoff, run the V2 suite once:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_mm_v2_*.py"`
- When shared production code changes, run the full suite once, including relevant Lighter/Grid regressions:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
  A full run on the same code covers the V2/focused tests it contains; do not rerun them just to check separate boxes. Repeat only for changed code, new failures, or unresolved concerns.
- Compare unrelated failures with the documented baseline; do not repair unrelated Grid failures during MM work. Preserve V2 execution-safety coverage without recreating the deleted V1 suite.
- Daily local network validation is a bounded 5-minute dry when the changed path needs it, not after every edit. `--allow-delayed-dry-book` is dry-only and explicitly labeled; live retains strict source checks. The unfinished 30-minute strict T3 is longer-duration evidence, not a prerequisite for local coding or preparing the minimal canary; dry results never prove live economics.

## Evidence and Git

- Keep phase/run evidence in the single `docs/mm_v2/EXPERIMENT_LOG.md`; update current status in existing docs instead of adding campaign/checkpoint layers. Historical failures remain evidence. No new framework or hard file-size gate is required to finish a change.
- Inspect the worktree and preserve unrelated user changes. Commit/push only when requested; an authorization already used for a one-time commit/push does not repeat itself. Never force-add ignored secrets/configs/logs.
- At handoff, distinguish local completion, remaining live/economic validation, and any real blocker. When Git work was requested, report branch, SHA, validation, and push status.
