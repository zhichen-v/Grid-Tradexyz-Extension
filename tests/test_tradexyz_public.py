"""
TradeXYZ 公開行情測試腳本

測試項目：
1. allMids - 取得所有 XYZ 資產中間價
2. l2Book  - 取得 TSLA 訂單簿
3. candleSnapshot - 取得 TSLA K線數據
4. metaAndAssetCtxs - 取得 XYZ 市場元數據
5. predictedFundings - 取得預測資金費率
"""

import asyncio
import aiohttp
import json
import time
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.text import Text
from rich import box

BASE_URL = "https://api.hyperliquid.xyz"
console = Console()


async def post_info(session: aiohttp.ClientSession, payload: dict) -> dict:
    async with session.post(f"{BASE_URL}/info", json=payload) as resp:
        status = resp.status
        body = await resp.text()
        if status != 200:
            return {"_error": True, "_status": status, "_body": body[:500]}
        return json.loads(body)


async def test_all_mids(session) -> bool:
    console.print(Rule("[bold cyan]TEST 1  allMids — XYZ 所有資產中間價[/bold cyan]", style="cyan"))

    with console.status("[cyan]查詢 allMids...[/cyan]", spinner="dots"):
        result = await post_info(session, {"type": "allMids", "dex": "xyz"})

    if isinstance(result, dict) and "_error" in result:
        console.print(f"  [red]FAIL: HTTP {result['_status']}[/red]\n  {result['_body']}")
        return False
    if not isinstance(result, dict):
        console.print(f"  [red]FAIL: 預期 dict，實際 {type(result).__name__}[/red]")
        return False

    sample_key = list(result.keys())[0] if result else ""
    prefix = "xyz:" if sample_key.startswith("xyz:") else ""

    us_stocks  = ["TSLA", "AAPL", "NVDA", "AMZN", "MSFT", "META", "GOOGL"]
    commodities = ["GOLD", "SILVER", "WTIOIL"]
    indices    = ["SP500", "XYZ100"]

    def build_section(symbols, title, color):
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2), title=f"[{color}]{title}[/{color}]")
        t.add_column(style="bold white", min_width=10)
        t.add_column()
        for sym in symbols:
            price = result.get(f"{prefix}{sym}")
            if price:
                t.add_row(sym, f"[green]${price}[/green]")
            else:
                t.add_row(sym, "[dim red]NOT FOUND[/dim red]")
        return t

    console.print(f"  共返回 [bold]{len(result)}[/bold] 個資產  ·  Key 格式: [dim]{sample_key!r}[/dim]")
    console.print(build_section(us_stocks,   "美股",  "yellow"))
    console.print(build_section(commodities, "商品",  "yellow"))
    console.print(build_section(indices,     "指數",  "yellow"))

    found = sum(1 for k in result if any(s in k for s in us_stocks))
    console.print(f"  [green]PASS[/green]  {len(result)} 個資產  ·  美股命中 {found}/{len(us_stocks)}\n")
    return True


async def test_l2_book(session) -> bool:
    console.print(Rule("[bold cyan]TEST 2  l2Book — TSLA 訂單簿[/bold cyan]", style="cyan"))

    with console.status("[cyan]查詢 l2Book...[/cyan]", spinner="dots"):
        result = await post_info(session, {
            "type": "l2Book", "coin": "xyz:TSLA",
            "nSigFigs": 5, "dex": "xyz"
        })

    if isinstance(result, dict) and "_error" in result:
        console.print(f"  [red]FAIL: HTTP {result['_status']}[/red]\n  {result['_body']}")
        return False

    levels = result.get("levels", [])
    if len(levels) < 2:
        console.print(f"  [red]FAIL: levels 長度 {len(levels)}，預期 >= 2[/red]")
        console.print(f"  {json.dumps(result)[:500]}")
        return False

    bids, asks = levels[0], levels[1]
    display = min(5, max(len(bids), len(asks)))

    book = Table(box=box.SIMPLE_HEAD, padding=(0, 3))
    book.add_column("Bid Qty",  style="green",  justify="right")
    book.add_column("Bid Price", style="bold green", justify="right")
    book.add_column("Ask Price", style="bold red",   justify="left")
    book.add_column("Ask Qty",  style="red",   justify="left")

    for i in range(display):
        b = bids[i] if i < len(bids) else {}
        a = asks[i] if i < len(asks) else {}
        book.add_row(
            str(b.get("sz", "")), f"${b.get('px', '')}",
            f"${a.get('px', '')}", str(a.get("sz", ""))
        )

    console.print(book)
    console.print(f"  [green]PASS[/green]  {len(bids)} bids  ·  {len(asks)} asks\n")
    return True


async def test_candles(session) -> bool:
    console.print(Rule("[bold cyan]TEST 3  candleSnapshot — TSLA 1h K線[/bold cyan]", style="cyan"))

    now_ms   = int(time.time() * 1000)
    start_ms = now_ms - 24 * 3600 * 1000

    with console.status("[cyan]查詢 candleSnapshot...[/cyan]", spinner="dots"):
        result = await post_info(session, {
            "type": "candleSnapshot",
            "req": {"coin": "xyz:TSLA", "interval": "1h",
                    "startTime": start_ms, "endTime": now_ms}
        })

    if isinstance(result, dict) and "_error" in result:
        console.print(f"  [red]FAIL: HTTP {result['_status']}[/red]\n  {result['_body']}")
        return False
    if not isinstance(result, list):
        console.print(f"  [red]FAIL: 預期 list，實際 {type(result).__name__}[/red]")
        return False

    console.print(f"  共返回 [bold]{len(result)}[/bold] 根 K線")

    if result:
        c = result[-1]
        ts = datetime.fromtimestamp(c.get("t", 0) / 1000).strftime("%Y-%m-%d %H:%M")
        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2), title="[yellow]最新一根 K線[/yellow]")
        t.add_column(style="dim", min_width=8)
        t.add_column(style="bold white")
        t.add_row("Time",  ts)
        t.add_row("Open",  f"${c.get('o')}")
        t.add_row("High",  f"[green]${c.get('h')}[/green]")
        t.add_row("Low",   f"[red]${c.get('l')}[/red]")
        t.add_row("Close", f"${c.get('c')}")
        t.add_row("Vol",   str(c.get("v")))
        console.print(t)

    console.print(f"  [green]PASS[/green]  {len(result)} candles\n")
    return True


async def test_meta(session) -> bool:
    console.print(Rule("[bold cyan]TEST 4  metaAndAssetCtxs — XYZ 市場元數據[/bold cyan]", style="cyan"))

    with console.status("[cyan]查詢 metaAndAssetCtxs...[/cyan]", spinner="dots"):
        result = await post_info(session, {"type": "metaAndAssetCtxs", "dex": "xyz"})

    if isinstance(result, dict) and "_error" in result:
        console.print(f"  [red]FAIL: HTTP {result['_status']}[/red]\n  {result['_body']}")
        return False

    if isinstance(result, list) and len(result) >= 2:
        meta, asset_ctxs = result[0], result[1]
        universe = meta.get("universe", [])

        t = Table(box=box.SIMPLE_HEAD, padding=(0, 2), title="[yellow]前 5 個資產[/yellow]")
        t.add_column("name",         style="bold white")
        t.add_column("maxLeverage",  style="cyan",  justify="right")

        for asset in universe[:5]:
            t.add_row(asset.get("name", "?"), str(asset.get("maxLeverage", "?")))
        console.print(t)

        if asset_ctxs:
            ctx = asset_ctxs[0]
            c = Table(box=box.SIMPLE, show_header=False, padding=(0, 2), title="[yellow]第一個 AssetCtx[/yellow]")
            c.add_column(style="dim")
            c.add_column(style="white")
            c.add_row("funding",       str(ctx.get("funding")))
            c.add_row("openInterest",  str(ctx.get("openInterest")))
            c.add_row("markPx",        str(ctx.get("markPx")))
            console.print(c)

        console.print(f"  [green]PASS[/green]  {len(universe)} assets  ·  {len(asset_ctxs)} ctxs\n")
        return True

    elif isinstance(result, dict):
        universe = result.get("universe", result.get("meta", {}).get("universe", []))
        console.print(f"  Universe: [bold]{len(universe)}[/bold] 個資產")
        console.print(f"  [green]PASS[/green]  元數據正常\n")
        return True

    else:
        console.print(f"  [yellow]WARN: 未知格式 {type(result).__name__}[/yellow]")
        console.print(f"  {json.dumps(result)[:500]}")
        return False


async def test_predicted_fundings(session) -> bool:
    console.print(Rule("[bold cyan]TEST 5  predictedFundings — XYZ 預測資金費率[/bold cyan]", style="cyan"))

    with console.status("[cyan]查詢 predictedFundings...[/cyan]", spinner="dots"):
        result = await post_info(session, {"type": "predictedFundings", "dex": "xyz"})

    if isinstance(result, dict) and "_error" in result:
        console.print(f"  [red]FAIL: HTTP {result['_status']}[/red]\n  {result['_body']}")
        return False

    if isinstance(result, list):
        t = Table(box=box.SIMPLE_HEAD, padding=(0, 2), title="[yellow]前 5 筆[/yellow]")
        t.add_column("asset", style="bold white")
        t.add_column("funding", style="cyan", justify="right")

        for item in result[:5]:
            if isinstance(item, list) and len(item) >= 2:
                t.add_row(str(item[0]), str(item[1]))
            elif isinstance(item, dict):
                t.add_row(str(item), "")

        console.print(t)
        console.print(f"  [green]PASS[/green]  {len(result)} 個資產\n")
        return True
    else:
        console.print(f"  Response type: {type(result).__name__}")
        console.print(f"  {json.dumps(result)[:300]}")
        return False


async def main():
    console.print()
    console.print(Panel(
        Text(
            f"TradeXYZ 公開行情 API 測試\n"
            f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ·  {BASE_URL}[/dim]",
            justify="center", style="bold cyan"
        ),
        border_style="cyan", padding=(0, 4)
    ))
    console.print()

    results: dict[str, bool] = {}

    async with aiohttp.ClientSession(headers={"Content-Type": "application/json"}) as session:
        results["allMids"]           = await test_all_mids(session)
        results["l2Book"]            = await test_l2_book(session)
        results["candles"]           = await test_candles(session)
        results["meta"]              = await test_meta(session)
        results["predictedFundings"] = await test_predicted_fundings(session)

    # ── 彙總 ──────────────────────────────────────────────────────────
    console.print(Rule("[bold]測試結果彙總[/bold]", style="dim"))
    summary = Table(box=box.SIMPLE_HEAD, padding=(0, 3))
    summary.add_column("測試項目", style="bold white")
    summary.add_column("結果", justify="center")

    for name, passed in results.items():
        summary.add_row(name, "[green]PASS[/green]" if passed else "[red]FAIL[/red]")

    console.print(summary)

    total  = len(results)
    passed = sum(1 for v in results.values() if v)
    failed = total - passed

    if failed == 0:
        console.print(Panel(
            Text(f"全部 {total} 項通過  —  TradeXYZ 美股公開行情 API 運作正常", justify="center", style="bold green"),
            border_style="green", padding=(0, 2)
        ))
    else:
        console.print(Panel(
            Text(f"通過 {passed} / {total}  ·  失敗 {failed} 項", justify="center", style="bold red"),
            border_style="red", padding=(0, 2)
        ))

    console.print()


if __name__ == "__main__":
    asyncio.run(main())
