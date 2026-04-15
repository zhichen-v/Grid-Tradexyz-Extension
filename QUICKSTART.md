# Quick Start

This is the shortest path to get the repository running.
If you want the full explanation, read `README.md`.

## 1. Enter the Project Directory

```bash
cd grid1.3
```

## 2. Create and Activate `.venv`

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

## 3. Install `uv` and Dependencies

```bash
python -m pip install uv
uv pip install -r requirements.txt
```

## 4. Configure TradeXYZ Credentials

Edit `config/exchanges/tradexyz_config.yaml`:

```yaml
tradexyz:
  authentication:
    private_key: "0xYOUR_PRIVATE_KEY"
    wallet_address: "0xYOUR_WALLET_ADDRESS"
```

If you use an agent wallet instead, you can also run:

```bash
uv run python setup_agent_wallet.py
```

## 5. Pick a Starter Config

Recommended first run:

- `config/grid/tradexyz_test_follow_long.yaml`

Alternative fixed-range example:

- `config/grid/tradexyz_test_long.yaml`

## 6. Start the Bot

Normal mode:

```bash
uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml
```

Debug mode:

```bash
uv run python run_grid_trading.py config/grid/tradexyz_test_follow_long.yaml --debug
```

Exit with `Ctrl+C` or `Q`.

## 7. Optional Smoke Checks

Public API:

```bash
uv run python test_tradexyz_public.py
```

Order placement:

```bash
uv run python test_tradexyz_order.py
```

Order cancel:

```bash
uv run python test_tradexyz_cancel_orders.py
```

These scripts may hit live endpoints. Use caution.

## 8. If Startup Fails

Check these first:

- `.venv` is activated
- `uv pip install -r requirements.txt` completed successfully
- `config/exchanges/tradexyz_config.yaml` contains valid credentials
- The config path you passed to `run_grid_trading.py` exists

## 9. Recommended Next Step

After your first successful startup, read `README.md` for:

- config structure
- supported runtime commands
- smoke-check usage
- troubleshooting details
- security notes
