# Market Maker 現況與優化方向

> 更新日期：2026-08-26。本文只做簡要總覽；實際啟停、硬閘門與最新證據以 [Market Maker MVP 操作指南](market_maker_mvp_operating_guide.md) 第 12 節及 fresh authenticated reads 為準。

## 現況

Market Maker MVP 已與 Grid runtime 分離，入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`。本輪沒有修改 Grid production code或 shared Lighter adapter。

目前具備：

- 單一 Lighter 永續合約、每側最多一張 managed order，雙向或單向 `POST_ONLY` 報價。
- 正規化 external BBO、fixed spread、inventory skew、fee-aware exit floor與 surplus-budgeted timed soft exit。
- Worst-case exposure、position cap、reduce-only風險收斂與 oversized reduce-only dust cleanup。
- Stale／invalid data、unknown order、uncertain mutation、reconciliation failure、account state不可信與drawdown超限時 fail closed。
- In-process authenticated account audit：唯一maker fills、exact fee、flat-to-flat turnover／net、equity、fee tier、`1x cross`與帳戶獨占。
- Cancel-before-replace、mutation limiter、WS／REST同步及 shutdown cancel／final audit。
- Soft-exit 盈餘只有在最新 processed fill 之後完成新 authenticated audit 才可信；startup retry 會重建 session baseline。
- Mutation budget不足時保留 reduce-only最高優先權；一般雙邊報價則依目前倉位先建立降低庫存的一側，避免固定BUY-first在long時先增加風險。

## 現行 long-run 候選

`both / 250 ticks / order 0.00020 / max position 0.00040 / 1x cross / max drawdown 0.50 USDG / mutation 8 per minute`

其他關鍵值：

- maker fee `1.2 bps`；economic gate 至少 `30 completed maker fills`。
- fee cover `>=1`，completed net／turnover `>=+0.02 bps`。
- timed soft exit `120s`，保留已實現盈餘 `0.02 bps`。
- account audit `15s`、外層 timeout `10s`、瞬時讀取 `5 attempts × 1s`。
- Source YAML 永遠保持 `dry_run: true`；受 ignore 保護的 live copy 只能多出 `dry_run: false`。

## 最新判定

- 前一live fingerprint的 `250 ticks / 8 mutations` 搭配降低庫存側優先修復，約29分鐘完成38筆／15輪：turnover `690.898688`、net `+0.00206015744`、`+0.0298185 bps`、cover `1.0248488`、約 `1416 USDG/h`／`77.9 fills/h`、max DD `0.031468`，自然flat後equity gate通過。
- `10 mutations/min` 單變因canary約11分鐘完成22筆／10輪；completed episodes為 `+0.0367967 bps`、cover `1.0306639`，runtime wall-rate（含第23筆open authenticated fill）約 `2005 USDG/h`／`127.8 fills/h`。但未達30筆即依操作者要求停止，且有1次已精確解決的cancellation terminal-visibility race，因此不是promotion證據。
- 停止窗口留下short `0.00020`，其後只用maker reduce-only於`78517.7`回補。Authenticated trade-ledger含此開放episode／cleanup的10/min全場結果為net `-0.00313897280`、`-0.0833743 bps`、cover `0.9305214`；baseline／final equity `299.558978 / 299.555839`，與ledger只差`2.72e-8 USDG`，故**本場完整economics為NO-GO**。10/min吞吐比較仍因22<30且非自然flat而INCONCLUSIVE；正式設定已回到 `8/min`，不能解讀為已證明10較差。
- 2026-08-26 08:49兩次authenticated postflight皆為position／orders／unresolved `0/0/0`，process `0`、collateral `299.555839 USDG`；目前策略已暫停且沒有掛單。
- 最後審查確認OrderManager現有 `position_refresh_required` 同時代表純cancel refresh與visible fill；在未拆出精確 `fill_observed` 前，不能安全加入REST同淨倉位fills的economics invalidation。匆忙實驗未納入本checkpoint，列為下一輪P0。
- Market Maker回歸 `278/278 OK`；full repo `515`仍是既知Grid／selective-cancel baseline `8 failures + 4 errors`，本輪沒有新增失敗，也未修改Grid production或shared Lighter adapter。

## 下一步

1. P0先拆分精確 `fill_observed` 與純position refresh，補同淨倉位fills／純reprice兩個反例；未完成前不進long-run。
2. P1再統一RiskManager與Strategy的inventory age／soft-exit latch並補recovery timer drift測試；現況只會保守延後退出，但會影響換手。
3. 完成P0／P1與offline regression後重算fingerprint、做相稱T3，再從fresh flat preflight以該fingerprint／`8/min`重跑至少30 completed且自然flat的短gate；通過後才執行多小時long-run。
4. 若要繼續評估 `10/min`，必須另開乾淨單變因場次，至少30 completed且自然flat；比較block／hour、terminal race密度、turnover／hour與**全場**fee cover後才可promotion。
5. 單向行情若仍造成長時間cap inventory，再研究MM-only momentum／toxicity entry guard；不以同輪縮spread或放大風險取代診斷。
6. 完成前維持source YAML `dry_run: true`與mutation `8/min`。VPS仍不在本階段。

不以 taker／IOC、self-trade、放大size／position／leverage／drawdown或隱藏前輪損失換取成交量。
