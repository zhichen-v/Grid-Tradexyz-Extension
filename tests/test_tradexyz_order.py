"""
TradeXYZ NVDA 掛單測試（使用 Agent Wallet）

前置條件:
  - 已執行 setup_agent_wallet.py 完成授權
  - .env 檔案中含有 HL_AGENT_KEY / HL_WALLET_ADDRESS

流程:
  1. 從 .env 載入 Agent 金鑰
  2. 查詢 NVDA 目前市場中間價
  3. 以低於市價 10% 掛 limit buy（不會立即成交）
  4. 確認掛單存在
  5. 取消訂單並確認

執行:
    uv run python -m tests.test_tradexyz_order
"""

import os
import sys
import time
from pathlib import Path

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.columns import Columns
from rich import box

BASE_URL = "https://api.hyperliquid.xyz"
console = Console()


def load_env():
    env_path = Path(".env")
    if not env_path.exists():
        console.print(Panel(
            "[red]找不到 .env 檔案\n請先執行: [bold]uv run python setup_agent_wallet.py[/bold][/red]",
            title="[red]ERROR[/red]", border_style="red"
        ))
        sys.exit(1)

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

    agent_key = os.environ.get("HL_AGENT_KEY", "")
    wallet_address = os.environ.get("HL_WALLET_ADDRESS", "")

    if not agent_key or not wallet_address:
        console.print(Panel(
            "[red].env 中缺少 HL_AGENT_KEY 或 HL_WALLET_ADDRESS\n"
            "請先執行: [bold]uv run python setup_agent_wallet.py[/bold][/red]",
            title="[red]ERROR[/red]", border_style="red"
        ))
        sys.exit(1)

    return agent_key, wallet_address


def main():
    # ── Header ────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        Text("TradeXYZ NVDA 掛單測試  ·  Agent Wallet", justify="center", style="bold cyan"),
        border_style="cyan", padding=(0, 4)
    ))

    # ── Step 0: 載入設定 ───────────────────────────────────────────────
    console.print(Rule("[bold]載入設定[/bold]", style="dim"))
    with console.status("[cyan]讀取 .env ...[/cyan]", spinner="dots"):
        agent_key, wallet_address = load_env()
        agent_account = eth_account.Account.from_key(agent_key)
        time.sleep(0.3)

    cfg_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    cfg_table.add_column(style="dim")
    cfg_table.add_column(style="green")
    cfg_table.add_row("主錢包", f"{wallet_address[:10]}…{wallet_address[-6:]}")
    cfg_table.add_row("Agent ", f"{agent_account.address[:10]}…{agent_account.address[-6:]}")
    console.print(cfg_table)

    # ── 建立客戶端 ─────────────────────────────────────────────────────
    import requests as _req

    info = Info(BASE_URL, skip_ws=True)
    exchange = Exchange(
        agent_account,
        BASE_URL,
        account_address=wallet_address,
        perp_dexs=["xyz"],
    )

    def fetch_open_orders():
        resp = _req.post(
            f"{BASE_URL}/info",
            json={"type": "frontendOpenOrders", "user": wallet_address, "dex": "xyz"},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        return resp.json() if resp.ok else []

    # ── Step 1: 清場 ───────────────────────────────────────────────────
    console.print(Rule("[bold]STEP 0  取消所有現有 XYZ 掛單[/bold]", style="dim"))
    with console.status("[cyan]查詢現有掛單...[/cyan]", spinner="dots"):
        existing = fetch_open_orders()

    if not existing:
        console.print("  [dim]目前無掛單[/dim]")
    else:
        cancel_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
        cancel_table.add_column("coin", style="yellow")
        cancel_table.add_column("oid", style="cyan")
        cancel_table.add_column("result")
        for o in existing:
            oid = o.get("oid")
            coin = o.get("coin", "")
            with console.status(f"[cyan]取消 {coin} oid={oid}...[/cyan]", spinner="dots"):
                cancel_r = exchange.cancel(name=coin, oid=oid)
            statuses = cancel_r.get("response", {}).get("data", {}).get("statuses", [])
            result = statuses[0] if statuses else cancel_r
            ok = result == "success"
            cancel_table.add_row(
                coin, str(oid),
                f"[green]success[/green]" if ok else f"[red]{result}[/red]"
            )
        console.print(cancel_table)

    # ── Step 2: 查詢中間價 ─────────────────────────────────────────────
    console.print(Rule("[bold]STEP 1  查詢 NVDA 市場中間價[/bold]", style="dim"))
    with console.status("[cyan]取得 all_mids...[/cyan]", spinner="dots"):
        all_mids = info.all_mids(dex="xyz")

    mid_str = all_mids.get("xyz:NVDA") or all_mids.get("NVDA")
    if not mid_str:
        console.print(Panel(
            f"[red]找不到 NVDA\n可用 keys: {list(all_mids.keys())[:5]}[/red]",
            title="[red]FAIL[/red]", border_style="red"
        ))
        sys.exit(1)

    mid_price = float(mid_str)
    limit_price = round(mid_price * 0.90, 1)
    order_qty = 0.1

    price_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    price_table.add_column(style="dim")
    price_table.add_column(style="bold white")
    price_table.add_row("中間價",   f"${mid_price:.2f}")
    price_table.add_row("掛單價",   f"${limit_price:.2f}  [dim](市價 -10%)[/dim]")
    price_table.add_row("數量",     f"{order_qty}")
    console.print(price_table)

    # ── Step 3: 掛單 ───────────────────────────────────────────────────
    console.print(Rule(f"[bold]STEP 2  掛 Limit Buy @ ${limit_price} × {order_qty}[/bold]", style="dim"))
    with console.status("[cyan]送出訂單...[/cyan]", spinner="dots"):
        order_result = exchange.order(
            name="xyz:NVDA",
            is_buy=True,
            sz=order_qty,
            limit_px=limit_price,
            order_type={"limit": {"tif": "Gtc"}},
        )

    if isinstance(order_result, dict) and order_result.get("status") == "err":
        console.print(Panel(
            f"[red]{order_result.get('response')}[/red]",
            title="[red]FAIL[/red]", border_style="red"
        ))
        sys.exit(1)

    statuses = (
        order_result.get("response", {}).get("data", {}).get("statuses", [])
        if isinstance(order_result, dict) else []
    )

    if not statuses:
        console.print(Panel("[red]無法取得訂單狀態[/red]", title="[red]FAIL[/red]", border_style="red"))
        sys.exit(1)

    status = statuses[0]
    if "error" in status:
        console.print(Panel(f"[red]{status['error']}[/red]", title="[red]FAIL[/red]", border_style="red"))
        sys.exit(1)

    resting = status.get("resting", {})
    order_id = resting.get("oid")

    order_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    order_table.add_column(style="dim")
    order_table.add_column(style="bold green")
    order_table.add_row("狀態",    "[green]訂單建立成功[/green]")
    order_table.add_row("訂單 ID", str(order_id))
    console.print(order_table)

    # ── Step 4: 查詢掛單 ───────────────────────────────────────────────
    console.print(Rule("[bold]STEP 3  查詢目前 XYZ 掛單[/bold]", style="dim"))
    with console.status("[cyan]查詢掛單...[/cyan]", spinner="dots"):
        open_orders = fetch_open_orders()

    nvda_orders = [o for o in open_orders if "NVDA" in o.get("coin", "")]
    console.print(f"  XYZ 總掛單: [bold]{len(open_orders)}[/bold] 筆  /  NVDA: [bold]{len(nvda_orders)}[/bold] 筆")

    if nvda_orders:
        oo_table = Table(box=box.SIMPLE_HEAD, padding=(0, 2))
        oo_table.add_column("oid",   style="cyan")
        oo_table.add_column("side",  style="yellow")
        oo_table.add_column("price", style="white")
        oo_table.add_column("sz",    style="white")
        for o in nvda_orders:
            side = o.get("side", "")
            side_str = f"[green]{side}[/green]" if side == "B" else f"[red]{side}[/red]"
            oo_table.add_row(str(o.get("oid")), side_str, str(o.get("limitPx")), str(o.get("sz")))
        console.print(oo_table)

    # ── Step 5: 取消訂單 ───────────────────────────────────────────────
    if order_id:
        console.print(Rule(f"[bold]STEP 4  取消訂單 {order_id}[/bold]", style="dim"))
        with console.status("[cyan]送出取消請求...[/cyan]", spinner="dots"):
            cancel_result = exchange.cancel(name="xyz:NVDA", oid=order_id)

        cancel_statuses = (
            cancel_result.get("response", {}).get("data", {}).get("statuses", [])
            if isinstance(cancel_result, dict) else []
        )

        if cancel_statuses and cancel_statuses[0] == "success":
            console.print("  [green]訂單已成功取消[/green]")
        else:
            console.print(f"  [yellow]結果: {cancel_statuses}[/yellow]")

    # ── Footer ─────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        Text("測試完成", justify="center", style="bold green"),
        border_style="green", padding=(0, 4)
    ))
    console.print()


if __name__ == "__main__":
    main()
