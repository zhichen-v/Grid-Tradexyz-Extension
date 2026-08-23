# Repository Instructions

## Scope and navigation

- Make surgical, reviewable changes; state assumptions and measurable success criteria.
- Apply the `karpathy-guidelines` skill when writing, reviewing, or refactoring code.
- Use CodeGraph for definitions, callers, impact, and other structural questions. Use `rg` for literal text and filenames.
- If `.codegraph/` is absent or the index is not initialized, ask before running `codegraph init -i`.
- For Market Maker work, stay within `run_market_maker.py`, `core/services/market_maker/`, related configs, tests, and docs.
- Do not modify Grid production code, configs, or tests unless a demonstrated fatal shared-component conflict makes it unavoidable. Lighter adapters are shared; explain Grid impact and run relevant regressions if they change.

## Trading safety and secrets

- Default to read-only checks and `dry_run: true`. Live mutations, position flattening, and margin or leverage changes require explicit authorization for that run.
- Keep normal Market Maker quotes `POST_ONLY`; never create self-trading or wash-volume flows.
- Before live work, verify the network, account, symbol exclusivity, open orders, position, fee tier, and risk limits. After stopping, verify the process is gone and authenticated open orders are zero.
- Fail closed on stale or untrusted data, unknown orders, uncertain mutations, reconciliation failure, or monitor failure.
- Never print or commit private keys, tokens, wallet-profile contents, signer objects, or other credentials. Sanitized diagnostics may be inspected and reported.
- Do not commit `.env*`, logs, secret-bearing exchange configs, or local `config/market_maker/test_*.yaml` files. Commit sanitized example configs only.
- Keep financial YAML values as strings and financial calculations as `Decimal`.

## Tests and documentation

- Use the repository `.venv` and `unittest`.
- For Market Maker changes, run affected tests plus:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_market_maker_*.py"`
- Run relevant Lighter and Grid safety regressions when shared adapter code changes.
- For runtime or shared production changes, also run:
  `.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"`
- Compare unrelated pre-existing failures with the documented baseline; do not repair unrelated Grid failures during Market Maker work.
- Treat `docs/market_maker_mvp_operating_guide.md` as the operational safety source of truth. Update it whenever rollout state or evidence changes.

## Git

- Inspect the worktree first and preserve unrelated user changes.
- Keep commits scoped to the request. Never force-add ignored secrets, live configs, or logs.
- Commit or push only when requested; report the branch, commit SHA, tests, and remaining blockers.
