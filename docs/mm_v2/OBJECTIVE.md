# Market Maker V2 — Volume-first objective

> 狀態：Phase 6 已提交；Phase 7 replay、Unified 映射與10min dry smoke_03通過。T3_01／T3_02分別於約18.6／26.9分鐘停止；讀取429已未重現，但來源時間誤差仍阻擋30min dry gate。最終獨立authenticated position0/orders0、程序已退出；目前無run。下一步先驗證host/source時鐘誤差處理，再重跑T3；live另有mutation／exit讀取預算No-Go，不因flat dry部分通過自動promotion。完整任務以使用者的 [V2 rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md) 為準；本檔只固定產品契約，不另開優化路線。

## 目標與判定

2026-09-05 依使用者覆寫完成 V1 清除：必要 execution safety 已移入 V2，無舊 package 依賴；V2 345 tests PASS，全專案 619 tests 為同組既有 8 failures + 4 errors，無新增失敗。此離線驗證不改變上方 dry/live gate 狀態。

在 session all-in net cost、drawdown 與 hard inventory 限額內，提高 `maker_turnover_per_quote_hour`。正常狀態持續雙邊 `POST_ONLY` 報價，允許單筆虧損；持倉逾時、虧損、session deadline 或 stop 必須進入有界退出流程。允許為整場風險控制付出 taker flatten 成本，不再等待不虧損的自然 flat。

- Safety：unknown orders、unresolved mutations、reconciliation failures、self-trades、position-cap breaches 都必須為零；結束時 authenticated position / open orders 為 `0 / 0`。
- Liveness：stop/deadline 後須在有界期限內 authenticated flat；撤單成功不等於平倉成功。
- Volume：以 maker turnover / quote hour 為主，maker fills / quote hour、quote uptime 為輔；不以 completed episode 數作主要分母。
- Economics：納入全部 maker/taker fills、fees、final flatten，另列 funding/cashflow；只在可信的 final-flat 邊界判定 all-in net cost、fee cover、drawdown 與 flatten loss share。未平倉或資料不全不能宣稱通過。

## Fee floor 與報價

令 `f` 為 authenticated maker fee（每側 bps）、`e` 為 target net edge（完整 roundtrip bps）、`v` 為每側短期波動 buffer：

```text
half_spread_bps = f + e / 2 + v
full_spread_bps = 2f + e + 2v
```

每側至少 cover maker fee。歷史 2026-09-03 preflight 的 maker / taker 為 `1.2 / 3.5 bps`，所以 maker roundtrip fee floor 為 `2.4 bps`；這不是目前費率或 live 建議。每輪 live 仍須重新取得 authenticated fee。Phase 1 使用 `v = 0` 的 baseline 比較 spread / quote distance，並明示 **not a queue-fill backtest**；不由 BBO spread 單獨推論成交率或經濟 GO。

## 非目標

- 不延伸 V1 ping-pong、fee-aware breakeven exit、natural-flat gate、toxicity controller、campaign authority 或固定 per-episode reserve。
- 不做 Grid 改造、多層報價、MACD/NATR/ML、自成交或洗量。
- 不在短 canary 找到經濟可行點前建造 24h infrastructure；三組 target-edge candidates 仍無可行點時，停止參數擴張並回報策略／市場限制。

## 授權與邊界

Live、平倉及 margin/leverage 操作須逐場明確授權；commit/push 另依操作者當次指示。舊 V1 campaign 的剩餘風險預算不自動轉成 V2 授權。

V2 live 啟動時必須在任何連線／mutation 前要求 `--authorize-bounded-flatten`，並取得當場明確授權；非 flat start、unknown order 或不可信 account/market state 必須拒絕／暫停。授權涵蓋預先約定的 reducing LIMIT IOC、slippage、attempt/deadline 與 stop loss 範圍，不能由 YAML 關閉。

即使授權有界 IOC，也不能保證無流動性／失聯時成交。這類情況必須回報 liveness failure 與 residual inventory，不能提高風險上限或把 cancel-only 說成完成。

## 複雜度預算與進度

V2 起始策略 config 為計畫的 **18 個 leaf fields**；安全不變量由 profile/code 擁有，不另開開關。Orchestrator ≤500 LOC、QuotePolicy ≤350、InventoryGovernor ≤400、session analyzer ≤500、Phase 1 feasibility ≤500；函式盡量 ≤60 LOC。只保留所需 port、標準庫與公開契約測試。

按 Phase 0 → 1 → skeleton → ledger → quote → governor → runner → replay/dry → 授權 canary 逐步驗收。架構見 [ARCHITECTURE](ARCHITECTURE.md)，唯一 phase/run 記錄見 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。依 2026-09-05 使用者明確指示，Market Maker 工作樹只保留 V2；原本等待 4h／24h 才刪除 V1 的限制已被取代。V1 歷史仍可由 Git tag `mm-v1-guard-driven-20260903` 追溯；必要的訂單安全語義由 V2 自有 execution modules 與測試承接，不保留舊策略或相容入口。這不是 V2 live／economic GO，也不改變逐場授權要求。
