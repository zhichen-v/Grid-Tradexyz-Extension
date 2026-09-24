# Repository Instructions

## Scope and navigation

- Make surgical, reviewable changes; state assumptions and measurable success criteria.
- Apply the `karpathy-guidelines` skill when writing, reviewing, or refactoring code.
- Use CodeGraph for definitions, callers, impact, and other structural questions. Use `rg` for literal text and filenames.
- If `.codegraph/` is absent or the index is not initialized, ask before running `codegraph init -i`.
- `main` is Grid-only: `run_grid_trading.py`, `core/services/grid/`, `config/grid/`, the exchange adapters, and their supporting diagnostics, tests, and docs.
- Market Maker runtime/config/tests/docs have been removed from `main`; their source remains recoverable from Git at `5537f0f`. Future Market Maker work belongs on a separate branch or in a separately scoped package/project, only when requested. Do not merge its strategy runtime back into `main` or restore it as a working-tree archive.
- Preserve shared exchange-adapter safety fixes needed by Grid. Bring back any future shared fixes selectively, with review of Grid impact and relevant Grid/adapter regressions, rather than merging a Market Maker strategy wholesale.

## Trading safety and secrets

- Default to read-only checks and offline tests. Live starts, stops, orders, cancellations, position flattening, and margin or leverage changes require explicit authorization; code changes do not authorize deployment or interference with a running strategy.
- Before authorized live work, verify the network, account, symbol exclusivity, open orders, position, and configured risk limits. Never create self-trading or wash-volume flows.
- Normal Grid shutdown attempts to cancel managed orders and intentionally retains filled positions. Do not add an automatic flatten requirement. Verify process exit and cancellation evidence separately; absent open orders alone are not terminal cancellation proof, and cancel-only shutdown is not proof of a flat account.
- Fail closed on stale or untrusted data, unknown orders, uncertain mutations, reconciliation failure, or monitor failure.
- Preserve the partial-fill rule: update cumulative progress without creating a reverse order until the full logical order has filled. Do not blindly repeat uncertain submissions or infer exact exchange IDs from price/quantity matches.
- Never print or commit private keys, tokens, wallet-profile contents, signer objects, or other credentials. Sanitized diagnostics may be inspected and reported.
- Do not commit `.env*`, logs, secret-bearing exchange configs, or private live/test configs. Commit sanitized example configs only.
- Keep financial YAML values as strings and financial calculations as `Decimal`.

## Tests and documentation

- Use the repository `.venv` and `unittest`.
- Run affected Grid tests, including lifecycle, health-check, partial-fill, and cleanup regressions when those paths change. Useful focused commands:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_grid_*.py"`
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_order_health_checker_incident.py"`
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_lighter_*.py"`
- When shared production code changes, also run the offline suite:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
- On Linux, use `.venv/bin/python` for the equivalent commands. Network smoke scripts and live mutation checks are not offline regressions; do not execute them without explicit authorization.
- Tests/imports may initialize logging. Use an isolated temporary working directory with the repository on `PYTHONPATH` when existing local logs must be preserved; never run tests inside an active VPS strategy directory.
- Compare unrelated pre-existing failures with the documented baseline; do not silently repair unrelated behavior. Keep user-facing changes concise in `CHANGELOG.md` and update affected Grid/diagnostic docs.

## Git

- Inspect the worktree first and preserve unrelated user changes.
- Keep commits scoped to the request. Never force-add ignored secrets, live configs, or logs.
- Commit or push only when requested; report the branch, commit SHA, tests, and remaining blockers.
