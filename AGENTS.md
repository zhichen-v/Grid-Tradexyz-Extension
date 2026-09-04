# Repository Instructions

## Scope and navigation

- Make surgical, reviewable changes; state assumptions and measurable success criteria.
- Apply the `karpathy-guidelines` skill when writing, reviewing, or refactoring code.
- Use CodeGraph for definitions, callers, impact, and other structural questions. Use `rg` for literal text and filenames.
- If `.codegraph/` is absent or the index is not initialized, ask before running `codegraph init -i`.
- Market Maker is V2-only under the user's 2026-09-05 override, governed by `docs/CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md` and `docs/mm_v2/`. Do not restore the removed V1 runtime/config/tests/docs or Gate/toxicity/campaign pipeline.
- V2 scope: `run_volume_market_maker.py`, `core/services/market_maker_v2/`, `config/market_maker_v2/`, `scripts/mm_v2_feasibility.py`, `scripts/analyze_mm_v2_session.py`, related tests and docs. Proven order safety is owned by V2 `order_manager.py` and `execution_models.py`, behind narrow public ports; V2 must not import the removed V1 package.
- Do not modify Grid production code, configs, or tests unless a demonstrated fatal shared-component conflict makes it unavoidable. Lighter adapters are shared; explain Grid impact and run relevant regressions if they change.

## Trading safety and secrets

- Default to read-only checks and `dry_run: true`. Live mutations, position flattening, and margin or leverage changes require explicit authorization for that run.
- Keep normal Market Maker quotes `POST_ONLY`; never create self-trading or wash-volume flows.
- Before live work, verify the network, account, symbol exclusivity, open orders, position, fee tier, and risk limits. After stopping, verify the process is gone and authenticated open orders are zero.
- Fail closed on stale or untrusted data, unknown orders, uncertain mutations, reconciliation failure, or monitor failure.
- Never print or commit private keys, tokens, wallet-profile contents, signer objects, or other credentials. Sanitized diagnostics may be inspected and reported.
- Do not commit `.env*`, logs, secret-bearing exchange configs, or local Market Maker live/test configs. Commit sanitized example configs only.
- Keep financial YAML values as strings and financial calculations as `Decimal`.
- V2 live startup must require per-run bounded-flatten authorization before connecting or mutating. A stop/deadline must attempt the authorized bounded exit; never equate cancel-only shutdown with authenticated flat. Unknown execution state still blocks new mutations until reconciled.

## Tests and documentation

- Use the repository `.venv` and `unittest`.
- For V2 daily changes, run affected public-contract tests and the relevant execution/replay tests. At each V2 milestone, run:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_mm_v2_*.py"`
- V2 execution-safety regressions are part of `test_mm_v2_*.py`; do not recreate the deleted V1 policy suite.
- Run relevant Lighter and Grid safety regressions when shared adapter code changes.
- At V2 milestones, or when shared production code changes, also run:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
- Compare unrelated pre-existing failures with the documented baseline; do not repair unrelated Grid failures during Market Maker work.
- V2 authority is the rebuild plan and `docs/mm_v2/`; maintain a single `EXPERIMENT_LOG.md` for phase/run evidence instead of adding campaign/checkpoint documents. V1 history remains in Git, not a working-tree archive. Its removal does not grant live authorization or imply V2 economic promotion.

## Git

- Inspect the worktree first and preserve unrelated user changes.
- Keep commits scoped to the request. Never force-add ignored secrets, live configs, or logs.
- Commit or push only when requested; report the branch, commit SHA, tests, and remaining blockers.
