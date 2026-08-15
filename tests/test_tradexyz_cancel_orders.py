"""
TradeXYZ 掛單清理工具

用途：
  - 取消所有 XYZ 市場（美股/指數/商品）的掛單
  - 取消所有加密貨幣永續合約的掛單
  - 平倉所有持倉
  - 適合在測試後執行，確保帳戶乾淨

前置條件:
  - 已執行 setup_agent_wallet.py 完成授權
  - .env 檔案中含有 HL_AGENT_KEY / HL_WALLET_ADDRESS

執行:
    uv run python -m tests.test_tradexyz_cancel_orders
"""

import os
import sys
from pathlib import Path

import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.text import Text
from rich import box

BASE_URL = "https://api.hyperliquid.xyz"
console = Console()


def load_env():
    """從 .env 載入認證資訊"""
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


def fetch_open_orders(wallet_address: str, dex: str = None) -> list:
    """查詢掛單"""
    payload = {"type": "frontendOpenOrders", "user": wallet_address}
    if dex:
        payload["dex"] = dex
    resp = requests.post(
        f"{BASE_URL}/info",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    return resp.json() if resp.ok else []


def fetch_positions(wallet_address: str, dex: str = None) -> list:
    """查詢持倉"""
    payload = {"type": "clearinghouseState", "user": wallet_address}
    if dex:
        payload["dex"] = dex
    resp = requests.post(
        f"{BASE_URL}/info",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if not resp.ok:
        return []
    data = resp.json()
    positions = data.get("assetPositions", [])
    # 過濾有實際持倉的
    return [
        p for p in positions
        if float(p.get("position", {}).get("szi", "0")) != 0
    ]


def main():
    # ── Header ────────────────────────────────────────────────────────
    console.print()
    console.print(Panel(
        Text("TradeXYZ 掛單清理工具", justify="center", style="bold cyan"),
        border_style="cyan", padding=(0, 4)
    ))

    # ── 載入設定 ──────────────────────────────────────────────────────
    console.print(Rule("[bold]載入設定[/bold]", style="dim"))
    with console.status("[cyan]讀取 .env ...[/cyan]", spinner="dots"):
        agent_key, wallet_address = load_env()
        agent_account = eth_account.Account.from_key(agent_key)

    cfg_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    cfg_table.add_column(style="dim")
    cfg_table.add_column(style="green")
    cfg_table.add_row("主錢包", f"{wallet_address[:10]}...{wallet_address[-6:]}")
    cfg_table.add_row("Agent ", f"{agent_account.address[:10]}...{agent_account.address[-6:]}")
    console.print(cfg_table)

    # ── 建立客戶端 ────────────────────────────────────────────────────
    # XYZ 市場用 SDK
    exchange_xyz = Exchange(
        agent_account,
        BASE_URL,
        account_address=wallet_address,
        perp_dexs=["xyz"],
    )
    # 加密貨幣用另一個 Exchange（不帶 perp_dexs）
    exchange_crypto = Exchange(
        agent_account,
        BASE_URL,
        account_address=wallet_address,
    )

    total_cancelled = 0
    total_closed = 0

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: 取消 XYZ 市場掛單
    # ══════════════════════════════════════════════════════════════════
    console.print(Rule("[bold]STEP 1  取消 XYZ 市場掛單[/bold]", style="dim"))
    with console.status("[cyan]查詢 XYZ 掛單...[/cyan]", spinner="dots"):
        xyz_orders = fetch_open_orders(wallet_address, dex="xyz")

    if not xyz_orders:
        console.print("  [dim]XYZ 市場無掛單[/dim]")
    else:
        console.print(f"  找到 [bold]{len(xyz_orders)}[/bold] 筆 XYZ 掛單")
        result_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
        result_table.add_column("coin", style="yellow")
        result_table.add_column("side")
        result_table.add_column("price", style="white")
        result_table.add_column("size", style="white")
        result_table.add_column("result")

        for o in xyz_orders:
            oid = o.get("oid")
            coin = o.get("coin", "")
            side = o.get("side", "")
            side_str = f"[green]Buy[/green]" if side == "B" else f"[red]Sell[/red]"

            with console.status(f"[cyan]取消 {coin} oid={oid}...[/cyan]", spinner="dots"):
                try:
                    cancel_r = exchange_xyz.cancel(name=coin, oid=oid)
                    statuses = cancel_r.get("response", {}).get("data", {}).get("statuses", [])
                    result = statuses[0] if statuses else cancel_r
                    ok = result == "success"
                except Exception as e:
                    result = str(e)
                    ok = False

            result_table.add_row(
                coin, side_str, str(o.get("limitPx")), str(o.get("sz")),
                "[green]success[/green]" if ok else f"[red]{result}[/red]"
            )
            if ok:
                total_cancelled += 1

        console.print(result_table)

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: 取消加密貨幣掛單
    # ══════════════════════════════════════════════════════════════════
    console.print(Rule("[bold]STEP 2  取消加密貨幣掛單[/bold]", style="dim"))
    with console.status("[cyan]查詢加密貨幣掛單...[/cyan]", spinner="dots"):
        crypto_orders = fetch_open_orders(wallet_address)

    if not crypto_orders:
        console.print("  [dim]加密貨幣無掛單[/dim]")
    else:
        console.print(f"  找到 [bold]{len(crypto_orders)}[/bold] 筆加密貨幣掛單")
        result_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
        result_table.add_column("coin", style="yellow")
        result_table.add_column("side")
        result_table.add_column("price", style="white")
        result_table.add_column("size", style="white")
        result_table.add_column("result")

        for o in crypto_orders:
            oid = o.get("oid")
            coin = o.get("coin", "")
            side = o.get("side", "")
            side_str = f"[green]Buy[/green]" if side == "B" else f"[red]Sell[/red]"

            with console.status(f"[cyan]取消 {coin} oid={oid}...[/cyan]", spinner="dots"):
                try:
                    cancel_r = exchange_crypto.cancel(name=coin, oid=oid)
                    statuses = cancel_r.get("response", {}).get("data", {}).get("statuses", [])
                    result = statuses[0] if statuses else cancel_r
                    ok = result == "success"
                except Exception as e:
                    result = str(e)
                    ok = False

            result_table.add_row(
                coin, side_str, str(o.get("limitPx")), str(o.get("sz")),
                "[green]success[/green]" if ok else f"[red]{result}[/red]"
            )
            if ok:
                total_cancelled += 1

        console.print(result_table)

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: 平倉 XYZ 持倉
    # ══════════════════════════════════════════════════════════════════
    console.print(Rule("[bold]STEP 3  平倉 XYZ 持倉[/bold]", style="dim"))
    with console.status("[cyan]查詢 XYZ 持倉...[/cyan]", spinner="dots"):
        xyz_positions = fetch_positions(wallet_address, dex="xyz")

    if not xyz_positions:
        console.print("  [dim]XYZ 市場無持倉[/dim]")
    else:
        console.print(f"  找到 [bold]{len(xyz_positions)}[/bold] 筆 XYZ 持倉")
        result_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
        result_table.add_column("coin", style="yellow")
        result_table.add_column("side")
        result_table.add_column("size", style="white")
        result_table.add_column("entry", style="white")
        result_table.add_column("result")

        for p in xyz_positions:
            pos = p.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", "0"))
            entry_px = pos.get("entryPx", "?")
            side_str = f"[green]Long {szi}[/green]" if szi > 0 else f"[red]Short {szi}[/red]"

            # 平倉 = 反方向市價單
            is_buy = szi < 0  # 空倉要買回
            close_sz = abs(szi)

            with console.status(f"[cyan]平倉 {coin} {close_sz}...[/cyan]", spinner="dots"):
                try:
                    # 取得當前價格作為參考
                    info = Info(BASE_URL, skip_ws=True)
                    all_mids = info.all_mids(dex="xyz")
                    mid_str = all_mids.get(coin) or all_mids.get(f"xyz:{coin}", "0")
                    mid_price = float(mid_str)

                    # 用偏離市價的限價單模擬市價（確保成交）
                    if is_buy:
                        close_px = round(mid_price * 1.05, 1)  # 高於市價 5%
                    else:
                        close_px = round(mid_price * 0.95, 1)  # 低於市價 5%

                    close_result = exchange_xyz.order(
                        name=f"xyz:{coin}" if not coin.startswith("xyz:") else coin,
                        is_buy=is_buy,
                        sz=close_sz,
                        limit_px=close_px,
                        order_type={"limit": {"tif": "Ioc"}},  # 立即成交否則取消
                        reduce_only=True,
                    )
                    statuses = close_result.get("response", {}).get("data", {}).get("statuses", [])
                    ok = bool(statuses and ("filled" in statuses[0] or "resting" in statuses[0]))
                    result = "closed" if ok else str(statuses)
                except Exception as e:
                    result = str(e)
                    ok = False

            result_table.add_row(
                coin, side_str, str(abs(szi)), str(entry_px),
                "[green]closed[/green]" if ok else f"[red]{result}[/red]"
            )
            if ok:
                total_closed += 1

        console.print(result_table)

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: 平倉加密貨幣持倉
    # ══════════════════════════════════════════════════════════════════
    console.print(Rule("[bold]STEP 4  平倉加密貨幣持倉[/bold]", style="dim"))
    with console.status("[cyan]查詢加密貨幣持倉...[/cyan]", spinner="dots"):
        crypto_positions = fetch_positions(wallet_address)

    if not crypto_positions:
        console.print("  [dim]加密貨幣無持倉[/dim]")
    else:
        console.print(f"  找到 [bold]{len(crypto_positions)}[/bold] 筆加密貨幣持倉")
        result_table = Table(box=box.SIMPLE_HEAD, show_header=True, padding=(0, 2))
        result_table.add_column("coin", style="yellow")
        result_table.add_column("side")
        result_table.add_column("size", style="white")
        result_table.add_column("entry", style="white")
        result_table.add_column("result")

        for p in crypto_positions:
            pos = p.get("position", {})
            coin = pos.get("coin", "")
            szi = float(pos.get("szi", "0"))
            entry_px = pos.get("entryPx", "?")
            side_str = f"[green]Long {szi}[/green]" if szi > 0 else f"[red]Short {szi}[/red]"

            is_buy = szi < 0
            close_sz = abs(szi)

            with console.status(f"[cyan]平倉 {coin} {close_sz}...[/cyan]", spinner="dots"):
                try:
                    info = Info(BASE_URL, skip_ws=True)
                    all_mids = info.all_mids()
                    mid_str = all_mids.get(coin, "0")
                    mid_price = float(mid_str)

                    if is_buy:
                        close_px = round(mid_price * 1.05, 1)
                    else:
                        close_px = round(mid_price * 0.95, 1)

                    close_result = exchange_crypto.order(
                        name=coin,
                        is_buy=is_buy,
                        sz=close_sz,
                        limit_px=close_px,
                        order_type={"limit": {"tif": "Ioc"}},
                        reduce_only=True,
                    )
                    statuses = close_result.get("response", {}).get("data", {}).get("statuses", [])
                    ok = bool(statuses and ("filled" in statuses[0] or "resting" in statuses[0]))
                    result = "closed" if ok else str(statuses)
                except Exception as e:
                    result = str(e)
                    ok = False

            result_table.add_row(
                coin, side_str, str(abs(szi)), str(entry_px),
                "[green]closed[/green]" if ok else f"[red]{result}[/red]"
            )
            if ok:
                total_closed += 1

        console.print(result_table)

    # ── 驗證 ──────────────────────────────────────────────────────────
    console.print(Rule("[bold]驗證結果[/bold]", style="dim"))
    with console.status("[cyan]重新查詢...[/cyan]", spinner="dots"):
        remaining_xyz = fetch_open_orders(wallet_address, dex="xyz")
        remaining_crypto = fetch_open_orders(wallet_address)
        remaining_xyz_pos = fetch_positions(wallet_address, dex="xyz")
        remaining_crypto_pos = fetch_positions(wallet_address)

    verify_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    verify_table.add_column(style="dim")
    verify_table.add_column(style="bold white")
    verify_table.add_row("已取消掛單", f"{total_cancelled} 筆")
    verify_table.add_row("已平倉", f"{total_closed} 筆")
    verify_table.add_row("剩餘 XYZ 掛單", f"{len(remaining_xyz)} 筆")
    verify_table.add_row("剩餘加密貨幣掛單", f"{len(remaining_crypto)} 筆")
    verify_table.add_row("剩餘 XYZ 持倉", f"{len(remaining_xyz_pos)} 筆")
    verify_table.add_row("剩餘加密貨幣持倉", f"{len(remaining_crypto_pos)} 筆")
    console.print(verify_table)

    # ── Footer ────────────────────────────────────────────────────────
    remaining_total = (
        len(remaining_xyz) + len(remaining_crypto) +
        len(remaining_xyz_pos) + len(remaining_crypto_pos)
    )
    if remaining_total == 0:
        console.print(Panel(
            Text("帳戶已清理完畢", justify="center", style="bold green"),
            border_style="green", padding=(0, 4)
        ))
    else:
        console.print(Panel(
            Text(f"仍有 {remaining_total} 筆未清理，請手動檢查", justify="center", style="bold yellow"),
            border_style="yellow", padding=(0, 4)
        ))
    console.print()


if __name__ == "__main__":
    main()
