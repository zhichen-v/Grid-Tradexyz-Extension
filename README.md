# Grid Trading System

This repository contains a grid trading runtime centered on `run_grid_trading.py`.
It is currently used mainly with the `tradexyz` exchange adapter, and also contains
supporting code for other exchange integrations.

This README is written for someone starting from zero: clone the repo, install the
environment, configure credentials, choose a grid config, and run the bot.

## 1. What This Repository Contains

- Grid strategy execution entrypoint: `run_grid_trading.py`
- Exchange adapters: `core/adapters/exchanges/`
- Grid coordinator, engine, tracker, and TUI: `core/services/grid/`
- Exchange configs: `config/exchanges/`
- Grid configs: `config/grid/`
- Smoke scripts for TradeXYZ: `test_tradexyz_public.py`, `test_tradexyz_order.py`, `test_tradexyz_cancel_orders.py`
- Runtime logs: `logs/`

## 2. Prerequisites

Before you begin, prepare:

- Git
- Python 3.11 or newer
- A terminal environment that can activate a virtual environment
- Valid exchange credentials if you plan to connect to live endpoints

This project is expected to run inside `.venv`, and the default workflow uses `uv`.

## 3. Clone the Repository

```bash
git clone <your-repo-url>
cd grid1.3
```

If you downloaded the source as a zip, unpack it first and then enter the project directory.

## 4. Create and Activate the Virtual Environment

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If PowerShell blocks script execution, run PowerShell as yourself and allow local scripts for the current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 5. Install `uv` and Project Dependencies

Install `uv` first if your machine does not have it yet:

```bash
python -m pip install uv
```

Then install the project dependencies:

```bash
uv pip install -r requirements.txt
```

The main runtime dependencies include:

- `ccxt`
- `aiohttp`
- `websockets`
- `pyyaml`
- `rich`
- `hyperliquid-python-sdk`
- `lighter-sdk`
- `eth_account`

## 6. Configure Exchange Credentials

There are two practical ways to provide credentials.

### Option A: Put credentials in the exchange config file

Edit `config/exchanges/tradexyz_config.yaml`:

```yaml
tradexyz:
  authentication:
    private_key: "0xYOUR_PRIVATE_KEY"
    wallet_address: "0xYOUR_WALLET_ADDRESS"
```

This is the most direct setup path.

### Option B: Use environment variables or `.env`

The runtime reads environment variables first, then falls back to the exchange config file.

For `tradexyz`, the relevant variables are:

```env
TRADEXYZ_API_KEY=0xYOUR_PRIVATE_KEY
TRADEXYZ_API_SECRET=0xYOUR_PRIVATE_KEY
TRADEXYZ_WALLET_ADDRESS=0xYOUR_WALLET_ADDRESS
```

For Hyperliquid-style agent wallet flow, the runtime also supports:

```env
HL_AGENT_KEY=0xYOUR_AGENT_PRIVATE_KEY
HL_WALLET_ADDRESS=0xYOUR_MAIN_WALLET_ADDRESS
HL_AGENT_ADDRESS=0xYOUR_AGENT_ADDRESS
```

You can place these in a local `.env` file. The repository already ignores `.env`, so it should not be committed.

## 7. Optional: Create a TradeXYZ Agent Wallet

If your TradeXYZ flow uses an agent wallet, run:

```bash
uv run python setup_agent_wallet.py
```

What this script does:

- Reads or prompts for your main wallet credentials
- Approves a new agent wallet through the Hyperliquid / TradeXYZ flow
- Writes agent credentials into `.env`
- Clears the raw private key from `config/exchanges/tradexyz_config.yaml`

Use this only if you understand your exchange account model and want the runtime to trade through an agent wallet instead of the main wallet key.

## 8. Choose a Grid Config

The main ready-to-run examples are under `config/grid/`:

- `config/grid/tradexyz_test_follow_long.yaml`
- `config/grid/tradexyz_test_long.yaml`
- `config/grid/backpack_capital_protection_long.yaml`
- `config/grid/backpack_capital_protection_short.yaml`

For a first TradeXYZ run, start with one of these:

- `config/grid/tradexyz_test_follow_long.yaml`: dynamic follow-long grid on `NVDA`
- `config/grid/tradexyz_test_long.yaml`: fixed-range long grid on `NVDA`

Two additional files are currently git-ignored local configs:

- `config/grid/tradexyz_long.yaml`
- `config/grid/tradexyz_long_NVDA.yaml`

Those should be treated as local/private run configs rather than shared examples.

## 9. Understand the Minimum Config Fields

At minimum, a grid config needs a `grid_system` block with fields like:

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "follow_long"
  grid_interval: 5.0
  order_amount: 0.1
  quantity_precision: 1
```

### Follow grid example

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "follow_long"

  follow_grid_count: 20
  follow_timeout: 60
  follow_distance: 2
  price_offset_grids: 0

  grid_interval: 5.0
  order_amount: 0.1
  quantity_precision: 1
  fee_rate: "0.0001"
  order_health_check_interval: 60
```

### Fixed-range grid example

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "long"

  lower_price: 155.0
  upper_price: 195.0

  grid_interval: 5.0
  order_amount: 0.2
  quantity_precision: 1
  fee_rate: "0.0001"
  order_health_check_interval: 60
```

Common `grid_type` values:

- `long`
- `short`
- `follow_long`
- `follow_short`
- `martingale_long`
- `martingale_short`

## 10. Run the Bot

Normal mode:

```bash
uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml
```

Debug mode:

```bash
uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml --debug
```

What the startup flow does:

1. Loads the selected YAML config
2. Builds the grid runtime config
3. Connects the exchange adapter
4. Creates the strategy, engine, state, and coordinator
5. Places the startup grid orders
6. Starts the Rich terminal UI

Exit safely with `Ctrl+C` or `Q`.

## 11. Smoke Checks

Before placing real orders, use the smoke scripts to confirm connectivity and adapter behavior.

Public connectivity:

```bash
uv run python test_tradexyz_public.py
```

Order placement flow:

```bash
uv run python test_tradexyz_order.py
```

Cancel-order flow:

```bash
uv run python test_tradexyz_cancel_orders.py
```

These scripts may hit real exchange endpoints. Do not run them with production credentials unless you understand the effect.

## 12. Logs and Runtime Output

Runtime logs are written under `logs/`.

Useful places to look when debugging:

- `logs/`
- `core/services/grid/implementations/grid_engine_impl.py`
- `core/services/grid/implementations/order_health_checker.py`
- `core/services/grid/terminal_ui.py`

If you need more detail, rerun with `--debug`.

## 13. Common Problems

### Config file does not exist

You passed a path that `run_grid_trading.py` cannot find. Use a relative path from the repo root, for example:

```bash
uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml
```

### Credentials were not found

The runtime checks:

1. Environment variables / `.env`
2. `config/exchanges/<exchange>_config.yaml`

If both are empty, startup continues far enough to print warnings but trading cannot work correctly.

### Spot market does not support short mode

The entrypoint explicitly blocks short grid modes on spot markets.

### TUI starts but no useful exchange activity appears

Check:

- Your credentials
- The selected symbol
- The selected grid config
- Whether the exchange account has enough capital
- Whether the selected smoke test succeeds first

## 14. Project Layout

```text
grid1.3/
├─ config/
│  ├─ exchanges/
│  └─ grid/
├─ core/
│  ├─ adapters/
│  ├─ di/
│  └─ services/grid/
├─ logs/
├─ run_grid_trading.py
├─ setup_agent_wallet.py
├─ test_tradexyz_public.py
├─ test_tradexyz_order.py
└─ test_tradexyz_cancel_orders.py
```

## 15. Security Notes

- Do not commit private keys, wallet addresses, or `.env` secrets.
- Start with small size and non-critical capital.
- Treat all order-placement scripts as potentially live.
- Review your config carefully before running any grid strategy.

## 16. Recommended First Run

If you just want to confirm the repository works end to end, this is the shortest path:

1. Create `.venv` and activate it
2. Install `uv`
3. Run `uv pip install -r requirements.txt`
4. Fill in `config/exchanges/tradexyz_config.yaml`
5. Review `config/grid/tradexyz_test_follow_long.yaml`
6. Run `uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml --debug`

If that starts cleanly and the TUI appears, your environment is set up correctly.
