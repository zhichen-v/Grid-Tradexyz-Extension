# Market Maker V2 — Volume-first objective

> 狀態：2026-09-07 01:11，分步admission後新實盤000201在22分49秒失敗：maker602.901337 USDG、realized net−0.12995240744，尚有殘倉，all-in／fee-cover不可判定。01:11:08 authenticated唯讀確認BTC long0.00017／掛單0、cash297.938824540532；不是risk_capacity_exhausted正常停場。已修maker minimum被誤套至reducing IOC的退出契約，正常POST_ONLY minimum保留；完整V2 469項PASS，含BTC .00017／partial後.00001的多空runner退出及精確對帳，未實際平倉。API deferrals10／account race1，quota仍待改善。VPS暫緩，Grid不動；公平時間窗比較及證據界線見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)與計畫§19.8。

## 目標與判定

2026-09-05 依使用者覆寫完成 V1 清除：必要 execution safety 已移入 V2，無舊 package 依賴；V2 345 tests PASS，全專案 619 tests 為同組既有 8 failures + 4 errors，無新增失敗。此離線驗證不改變上方 dry/live gate 狀態。

在 session all-in net cost、drawdown 與 hard inventory 限額內，提高固定完整時間窗的 `maker_turnover_per_wall_hour`。正常狀態持續雙邊 `POST_ONLY` 報價，允許單筆虧損；持倉逾時、虧損、session deadline 或 stop 必須進入有界退出流程。允許為整場風險控制付出 taker flatten 成本，不再等待不虧損的自然 flat。

- Safety：unknown orders、unresolved mutations、reconciliation failures、self-trades、position-cap breaches 都必須為零；結束時 authenticated position / open orders 為 `0 / 0`。
- Liveness：stop/deadline 後須在有界期限內 authenticated flat；撤單成功不等於平倉成功。
- Volume：以 maker turnover / 固定完整時間窗為主，包含startup、撤換單、cooldown、pause與收尾；早停不縮短預定窗口，收尾超時延長分母。Quote-hour效率、雙邊working uptime、capital turnover作診斷；R4 analyzer提供這些比較，缺allocated capital或working-side證據時保持unavailable。
- Economics：納入全部 maker/taker fills、fees、final flatten，另列 funding/cashflow；只在可信的 final-flat 邊界判定 all-in net cost、fee cover、drawdown 與 flatten loss share。未平倉或資料不全不能宣稱通過。

使用者於2026-09-05提供測試資金約299 USDG，成交量目標尚未設定；不是本輪authenticated餘額或損失授權。先建立volume/cost frontier與實際可執行lot／inventory band表，再約定量級。正acquisition cost必須明示尚未fee-neutral，不可當成原目標通過。

## Fee floor 與報價

令 `f` 為 authenticated maker fee（每側 bps）、`e` 為 target net edge（完整 roundtrip bps）、`v` 為每側短期波動 buffer：

```text
half_spread_bps = f + e / 2 + v
full_spread_bps = 2f + e + 2v
```

每側相對reservation的half-spread至少為maker fee，屬首版報價baseline，**不保證每筆或整場cover fee**；不同時間的reference、inventory drift及退出成本仍需真實成交驗證。歷史 2026-09-03 preflight 的 maker / taker 為 `1.2 / 3.5 bps`，所以 maker roundtrip fee floor 為 `2.4 bps`；這不是目前費率或 live 建議。每輪 live 仍須重新取得 authenticated fee。Phase 1 使用 `v = 0` 的 baseline 比較 spread / quote distance，並明示 **not a queue-fill backtest**；不由 BBO spread 單獨推論成交率或經濟 GO。

## 非目標

- 不延伸 V1 ping-pong、fee-aware breakeven exit、natural-flat gate、toxicity controller、campaign authority 或固定 per-episode reserve。
- 不做 Grid 改造、多層報價、MACD/NATR/ML、自成交或洗量。
- 不在短 canary 找到經濟可行點前建造24h infrastructure；三組target-edge只作初篩，回報已測區域無可行點或證據不足，不以三場失敗宣稱整個市場不可能fee-neutral。聚合先加總再相除，所有預定窗口／失敗均保留，另以未參與選參的窗口確認。

## 授權與邊界

Live、平倉及 margin/leverage 操作須逐場明確授權；commit/push 另依操作者當次指示。舊 V1 campaign 的剩餘風險預算不自動轉成 V2 授權。

V2 live 啟動時必須在任何連線／mutation 前要求 `--authorize-bounded-flatten`，並取得當場明確授權；非 flat start、unknown order 或不可信 account/market state 必須拒絕／暫停。授權涵蓋預先約定的 reducing LIMIT IOC、slippage、attempt/deadline 與 stop loss 範圍，不能由 YAML 關閉。

即使授權有界 IOC，也不能保證無流動性／失聯時成交。這類情況必須回報 liveness failure 與 residual inventory，不能提高風險上限或把 cancel-only 說成完成。

## 複雜度預算與進度

R1退出、R2雙邊報價／drawdown reserve／final freshness、R3 own/book對齊／arrival race／保守hold age及健康transport的API退出預留均已有離線契約驗收。日常短dry可明確放寬source至−100..10000ms；live拒絕該選項並保留strict檢查。頻繁改價仍可提前花到quota保留邊界，尚未證明持續大成交量。R4 analyzer493 LOC；真實BTC minimum base為0.00020，因此0.00026半單仍無法保持soft邊界雙邊報價，較大數量表僅是算術假設。完整限制見rebuild plan §19.7及EXPERIMENT_LOG，不自動放大size或授權canary。

V2 起始策略 config 為計畫的 **18 個 leaf fields**；安全不變量由 profile/code 擁有，不另開開關。Orchestrator ≤500 LOC、QuotePolicy ≤350、InventoryGovernor ≤400、session analyzer ≤500、Phase 1 feasibility ≤500；函式盡量 ≤60 LOC。只保留所需 port、標準庫與公開契約測試。

按 Phase 0 → 1 → skeleton → ledger → quote → governor → runner → replay/dry → 授權 canary 逐步驗收。架構見 [ARCHITECTURE](ARCHITECTURE.md)，唯一 phase/run 記錄見 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。依 2026-09-05 使用者明確指示，Market Maker 工作樹只保留 V2；原本等待 4h／24h 才刪除 V1 的限制已被取代。V1 歷史仍可由 Git tag `mm-v1-guard-driven-20260903` 追溯；必要的訂單安全語義由 V2 自有 execution modules 與測試承接，不保留舊策略或相容入口。這不是 V2 live／economic GO，也不改變逐場授權要求。
