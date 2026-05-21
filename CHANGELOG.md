# Changelog

## [Unreleased] - 2026-04-08

### Added

#### 2026-05-21

- Added price-based grid stop loss settings: when enabled, long grids trigger at or below `stop_loss_price`, short grids trigger at or above it, then cancel open orders, market-close the current position, and stop the grid runtime.
- Documented stop loss settings in `config/grid/README.md` and filled `config/grid/example.yaml` with a complete English TradeXYZ example.

### Fixed

#### 2026-05-21

- Fixed TradeXYZ XYZ symbol routing so configured assets such as `MU` and `MU-USD` are recognized as XYZ markets instead of falling through to ccxt/Hyperliquid ticker calls, and grid startup now fails fast when exchange connection setup returns unsuccessful.
- Fixed paused/error-threshold grid recovery so health checks no longer cancel, restore, or place missing orders while the coordinator is paused, stopped, or resetting, and repeated fill-handling errors now stop the runtime and cancel open orders instead of leaving live orders unmanaged.

#### 2026-05-19

- Fixed short-grid scalping take-profit placement so aggregate buy TP orders are forced below the current market instead of becoming immediately marketable, blocked short-grid scalping from placing buy TP orders after the tracked position is no longer short, and stopped ambiguous TP retries once position sync shows the prior order likely filled.
- Fixed short-grid health-check TP coverage accounting so chained base-side sell re-entry orders are no longer counted as buy take-profit coverage, allowing real missing short TP coverage to be reported in the correct direction.

#### 2026-04-28

- Fixed TradeXYZ health-check/order-sync races where stale open-order snapshots could re-import already finalized reverse orders under the wrong grid id, causing base-grid gaps such as 209.25 to stay unfilled and duplicate same-price buys such as 208.95 to remain tracked as legitimate.
- Tightened duplicate cleanup so same price/side exchange orders keep only one order even when multiple local pending entries claim them, while still preferring a locally tracked order as the survivor.
- Adjusted inferred base-grid gap repair so live reverse orders protect only their source grid instead of blocking the base order at the reverse order's price level.

#### 2026-04-27

- Fixed health-check gap repair so locally tracked pending orders no longer count as exchange-open orders during missing-order detection, allowing stale local/state entries to trigger real replacement orders.
- Hardened health-check replacement order repair so expected orders are merged from coordinator state and engine cache, stale local base-grid entries no longer block inferred base repairs, live reverse orders still protect their source grid, repaired orders are synced back into coordinator state immediately, and duplicate cleanup ignores local ids that are no longer open on the exchange.
- Fixed TradeXYZ partial-fill continuation handling so REST fallback can finalize full fills from `userFills` snapshots, partial missing-order repairs replace only the remaining amount while preserving the original logical order size, and restored partial orders keep their fill ledger before placing reverse orders.

#### 2026-04-24

- Fixed grid health-check gap repair so duplicate-cleanup cancellations immediately refresh exchange order state before missing-order detection, preventing same-cycle false negatives after the checker removes an order itself.
- Narrowed the unresolved-order repair gate in `core/services/grid/implementations/order_health_checker.py` so unresolved final-status checks now defer only overlapping missing price/side slots instead of blocking every other gap repair in the cycle.
- Added persistent-gap fallback repair in `core/services/grid/implementations/order_health_checker.py`: when live open-order count drops below the configured grid target, the checker now rebuilds expected grid slots from the full ladder and can bypass the recent-fill cooldown for long-lived gaps.
- Tightened duplicate-order cleanup to preserve locally tracked pending orders first instead of canceling purely by the first-seen exchange `price + side` pair.
- Fixed startup fill reconciliation in `core/services/grid/coordinator/grid_coordinator.py` so fills that arrive before `state.active_orders` is populated can still be recovered from the engine pending-order cache and place the correct reverse take-profit order.
- Restricted gap repair in `core/services/grid/implementations/order_health_checker.py` to tracked orders plus inferred base-side maker gaps, removing the over-aggressive full-grid fallback that could manufacture extra sell/take-profit orders above the market.
- Clarified coordinator fill-reconciliation logs in `core/services/grid/coordinator/grid_coordinator.py` so startup races, engine-cache recovery, and likely manual/external intervention are labeled explicitly instead of all appearing as generic untracked-fill warnings.
- Fixed a startup crash in `core/services/grid/coordinator/grid_coordinator.py` by restoring the missing `Tuple` typing import used by the new fill-reconciliation log helper.
- Downgraded isolated take-profit coverage mismatches in `core/services/grid/implementations/order_health_checker.py` from immediate warnings to informational notes while order books and positions stay aligned, and only re-escalate them if the same TP-only mismatch persists across multiple health-check cycles.

#### 2026-04-22

- Fixed `PnL Statistics -> Realized` in `core/services/grid/terminal_ui.py` to use completed-cycle realized profit as `avg_cycle_profit * completed_cycles`, and recalculated the related TUI profit fields from the same basis.
- Hardened TradeXYZ partial-fill reconciliation so websocket `user_fill` events and health-check `userFills` snapshots share one per-order fill ledger and do not double-count the same execution.
- Fixed fill reconciliation so coordinator-side state must actually transition a pending order to filled before tracker statistics and reverse-order placement run, and added a same-grid/side/price fallback when the tracked `state.active_orders` key drifts from the live fill order id.
- Added higher-detail fill diagnostics around TradeXYZ `user_fill` deduplication, coordinator fill-entry keys, and state fallback reconciliation so repeated fills and stale-order mismatches can be traced directly from runtime logs.
- Fixed a regression in `GridState.mark_order_filled()` so successful state transitions return `True` again; without that return value coordinator fill handling exited early and stopped placing reverse take-profit orders.

#### 2026-04-21

- Fixed TUI position fields so `Position`, `Average Cost`, and `Position Value` are not overwritten by an empty `0 / 0` websocket cache when a synced REST snapshot already exists.
- Updated liquidation display to prefer the latest exchange liquidation price captured by `PositionMonitor`, and label the fallback calculation as an estimate.
- Updated `Position Value` to use the live mark/current price instead of cost basis.
- Moved managed logging to symbol-scoped directories such as `logs/NVDA/` so multiple tickers can run without clearing each other's logs.
- Delayed spot-reserve imports until after logging initialization in `run_grid_trading.py` so import-time logger setup no longer forces the shared root `logs/` directory.
- Reworked `core/__init__.py` to use lazy top-level exports and avoid recreating the shared root log directory during import.
- Isolated multi-ticker strategy accounting to the active symbol in coordinator-related modules and aligned TUI liquidation fallback with the new symbol-scoped equity path.

#### 2026-04-18

- Fixed `Trigger Statistics -> Completed cycles` so one completed cycle equals one buy fill paired with one sell fill, while average cycle profit continues to use completed profit cycles only.

#### 2026-04-16

- Fixed TradeXYZ partial-fill health-check fallback so `userFills` snapshots no longer mis-finalize partially filled grid orders as fully filled.
- Cleared existing `logs/*.log` files during managed logging initialization and fixed 1000-line rollover behavior so active logs reset cleanly instead of appearing stuck.

#### 2026-04-15 to 2026-04-10

- Reduced repeated TradeXYZ health-check log spam and false-positive runtime consistency warnings.
- Tightened take-profit coverage validation so unrelated exchange orders no longer distort local protection checks.
- Fixed `Avg cycle profit` in the terminal UI to use only completed reverse-order cycle profit and increased its precision from 2 to 4 decimal places.
- Suspended health-check repair actions during startup batch placement and fixed-range grid reset placement to avoid startup-time duplicate orders.
- Fixed false websocket timeout fallback in `GridEngineImpl` and improved websocket activity tracking during subscription recovery.
- Limited major runtime logs to the most recent 1000 lines and rebuilt the corrupted local `order_health_checker.py` into a compile-safe implementation.
- Fixed recent filled-order display in the terminal UI to render in UTC+8.
- Normalized the repaired logging and monitoring paths to English-only runtime messages.
- Fixed TradeXYZ partial-fill handling so incremental fills are accumulated correctly and do not prematurely close grid orders.
- Synced coordinator state position snapshots with the latest exchange-backed position data and tightened grid fill deduplication.
- Fixed top-bound reverse take-profit handling in `OrderHealthChecker` by comparing open orders with normalized `price + side` matching instead of lossy grid-id mapping.

### Changed

#### 2026-05-16

- Added named TradeXYZ agent wallet profiles: `setup_agent_wallet.py --wallet-name <name>` now writes `.env.wallets/<name>.env` without overwriting existing named profiles unless `--overwrite` is passed, and `run_grid_trading.py --wallet-name <name>` loads that profile explicitly for grid startup.
- Isolated wallet-profile runtime logs under `logs/<wallet>/<symbol>/` so multiple terminals using different wallets can run the same symbol without clearing each other's logs.
- Allowed `run_grid_trading.py` to accept bare grid config filenames such as `tradexyz_NVDA_long.yaml` and resolve them from `config/grid/`, while preserving full-path support.
- Updated `README.md` and `AGENTS.md` with named wallet profile usage, shortened grid config invocation, and wallet-profile credential safety notes.
- Preserved non-zero process exits for startup validation failures while keeping `--help` and `--version` behavior intact.
- Added direct wallet profile shortcuts such as `--main`, while keeping the legacy `--wallet-name <name>` form available.
- Copied the existing default `.env` credentials into `.env.wallets/main.env` for the new `--main` profile.

#### 2026-04-26

- Rewrote `config/grid/README.md` into a clean UTF-8 configuration guide, removing garbled text and expanding the documentation to match the current runtime-supported grid, protection, and nested YAML settings.

#### 2026-04-22

- Simplified this changelog by removing noisy per-file lists, duplicate entries, and overly detailed validation logs while keeping the actual behavior changes and key checks.
- Updated `AGENTS.md` to require concise, high-signal `CHANGELOG.md` maintenance and to avoid low-value `Files` sections, duplicate entries, and overly verbose validation logs by default.
- Normalized line endings in `core/services/grid/coordinator/grid_coordinator.py` from malformed `CRCRLF` sequences back to standard line breaks so IDEs no longer render alternating code and blank lines.
- Started coordinator-layer text cleanup under `core/services/grid/coordinator/`, including English-only rewrites for `__init__.py` and `verification_utils.py`.
- Continued coordinator text cleanup across `grid_reset_manager.py`, `position_monitor.py`, `order_operations.py`, `balance_monitor.py`, `scalping_operations.py`, and the primary runtime-facing text in `grid_coordinator.py`.
- Cleaned redundant blank lines in `core/services/grid/implementations/order_health_checker.py` without changing runtime logic.

#### 2026-04-19

- Simplified `Position Information` in the grid terminal UI to show only `Position`, `Average Cost`, `Position Value`, and `Liquidation risk`.
- Removed the `Controls` panel from the Rich TUI layout and rebalanced the remaining layout with adaptive section sizing.

#### 2026-04-15

- Rewrote `README.md` and `QUICKSTART.md` into cleaner English onboarding guides.
- Updated `AGENTS.md` with repository workflow guidance, including repository-relative paths by default and the `.venv` / `uv` execution flow.
- Cleaned `run_grid_trading.py` and `core/services/grid/terminal_ui.py` into English-only implementations and removed emoji-heavy or garbled runtime text.
- Rebuilt `core/services/grid/implementations/grid_engine_impl.py` into a single clean implementation and refined `OrderHealthChecker` restore, gap-repair, and unresolved-order handling.
- Disabled automatic position repair for `tradexyz` because the current exposure heuristic was too noisy.
- Strengthened health-check validation so success is no longer inferred from order-count parity alone.

### Validation

- 2026-05-21: `.\.venv\Scripts\python.exe -m py_compile run_grid_trading.py core\services\grid\models\grid_config.py core\services\grid\models\grid_metrics.py core\services\grid\coordinator\grid_coordinator.py core\services\grid\coordinator\grid_reset_manager.py core\services\grid\terminal_ui.py`
- 2026-05-21: Parsed all `config/grid/*.yaml` files with PyYAML and ran local stop-loss assertions for long and short trigger directions using `config/grid/example.yaml`.
- 2026-05-21: `.\.venv\Scripts\python.exe -m py_compile core\adapters\exchanges\adapters\tradexyz_base.py core\adapters\exchanges\adapters\tradexyz.py core\services\grid\implementations\grid_engine_impl.py`
- 2026-05-21: Local assertion script covering TradeXYZ `MU`, `MU-USD`, `MU/USD:PERP`, and `xyz:MU` routing to XYZ while keeping crypto symbols on the non-XYZ path; confirmed public `allMids` contains `xyz:MU` and `TradeXYZRest.get_ticker("MU-USD")` returns through the XYZ path.
- 2026-05-21: `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\order_health_checker.py core\services\grid\coordinator\grid_coordinator.py`
- 2026-05-21: Local assertion script covering paused/stopped/reset health-repair suspension and confirming missing-order restore is not attempted while repair is blocked.

- 2026-05-19: `.\.venv\Scripts\python.exe -m py_compile core\services\grid\scalping\scalping_manager.py core\services\grid\coordinator\scalping_operations.py`
- 2026-05-19: Local assertion script covering the TSLA short-grid scalping scenario where `current_price=414.64`, position `-0.6`, and prior realized profit previously produced a marketable buy TP above the market.
- 2026-05-19: Local assertion script covering short-grid TP coverage classification so buy reverse orders count as TP coverage while chained sell re-entry orders do not.

- 2026-05-16: `.\.venv\Scripts\python.exe -m py_compile setup_agent_wallet.py run_grid_trading.py`
- 2026-05-16: `uv run python run_grid_trading.py --help`
- 2026-05-16: `uv run python setup_agent_wallet.py --help`
- 2026-05-16: `uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml --wallet-name missing_profile_check`
- 2026-05-16: `uv run python run_grid_trading.py tradexyz_test_follow_long.yaml --wallet-name missing_profile_check`
- 2026-05-16: `uv run python run_grid_trading.py does_not_exist.yaml`
- 2026-05-16: `uv run python run_grid_trading.py does_not_exist.yaml --main`
- 2026-05-16: `uv run python setup_agent_wallet.py --main`

- `Get-Content -Raw -Encoding UTF8 config\grid\README.md`

- 2026-04-28: `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\grid_engine_impl.py core\services\grid\implementations\order_health_checker.py`
- 2026-04-28: Local assertion script covering reverse-order source-grid inference for 209.25 and duplicate same-side 208.95 cleanup.
- 2026-04-27: `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\order_health_checker.py`
- 2026-04-27: `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\grid_engine_impl.py core\services\grid\implementations\order_health_checker.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\__init__.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\verification_utils.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\grid_reset_manager.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\position_monitor.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\order_operations.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\balance_monitor.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\scalping_operations.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\coordinator\grid_coordinator.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\grid_engine_impl.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\order_health_checker.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\position_tracker_impl.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\terminal_ui.py`
- `.\.venv\Scripts\python.exe -m py_compile core\logging\logger.py core\logging\__init__.py run_grid_trading.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\models\grid_state.py core\services\grid\coordinator\grid_coordinator.py`
- `.\.venv\Scripts\python.exe -m py_compile core\services\grid\implementations\grid_engine_impl.py`
- Confirmed `core/services/grid/coordinator/grid_coordinator.py` no longer contains malformed `\r\r\n` line endings after normalization.

## [1.3.6] - 2026-04-08

### Fixed

- Reduced Rich TUI flicker and screen corruption caused by INFO-level background logs writing to the same terminal while `Live` was rendering.
- Fixed grid health check scheduling so `order_health_check_interval` is honored instead of being effectively capped by a hard-coded 60-second loop delay.
- Fixed the first health check log showing an absurd elapsed time by initializing the last-check timestamp when the task starts.

### Changed

- Added a temporary console log level override helper in `core.logging` so TUI mode can keep file logging unchanged while suppressing noisy terminal output.
- Updated `GridTerminalUI` to raise console logging to `WARNING` while Rich `Live` is active, then restore the previous console level on exit.
- Added repository workflow guidance in `AGENTS.md` requiring `CHANGELOG.md` to be read before code changes and updated after code changes.

### Validation

- `.\.venv\Scripts\python.exe -m py_compile core\logging\logger.py core\logging\__init__.py core\services\grid\terminal_ui.py core\services\grid\implementations\grid_engine_impl.py`
