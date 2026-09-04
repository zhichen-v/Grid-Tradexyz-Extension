# Market Maker V2 — Architecture contract

> 設計契約，逐階段實作。權威來源：[rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)。產品目標／fee floor／授權／complexity budget 見 [OBJECTIVE](OBJECTIVE.md)。Phase/run 驗收只記在 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

Phase 2 已有 immutable domain models、五個 ports、`dry_synthetic_cycle` 與 `LegacyDryExecutionPort`。相容層只接受 empty dry plan／simulated managed-order cancellation；非空報價、IOC 與 live config 明確拒絕。Unknown/unresolved 或非 simulated managed order 在 delegate 前封鎖；V1 strategy package 不會由純 V2 contracts/synthetic cycle 的 import 載入。

這不是 live-ready execution。`AccountPort` 目前由 synthetic fixture 驗收，不能把其 `authenticated=True` 當成真實帳戶證據。V1 AccountMonitor 內含舊 episode 經濟政策，因此不包裝它；現有 Adapter 沒有公開 current fee-schedule API，Phase 6 接線時需由窄 adapter layer 取得真正 authenticated account/fee truth，不可讀 private signer／REST client 或用歷史費率冒充。非空 quote reconciliation 的 V2 risk contract 留待後續 quote/governor 階段，bounded IOC 留待 Phase 5。

## 隔離與 ownership

V2 使用獨立的 `run_volume_market_maker.py`、`core/services/market_maker_v2/`、`config/market_maker_v2/` 與 `test_mm_v2_*.py`。不修改 V1 config/coordinator/order manager 去兼容 V2；不引入 V1 episode、toxicity、reason-string 或 private-slot 依賴。Grid production 不在本次範圍。

| 元件 | 唯一責任 |
|---|---|
| MarketState / MarketDataPort | 去除可識別 own volume 的 external BBO、有效 microprice（否則 mid）、有限長度 EWMA；拒絕 stale/untrusted 資料 |
| QuotePolicy | 由市場、inventory 與 governor constraints 產生每側至多一張正常 POST_ONLY 意圖；沒有交易副作用 |
| InventoryGovernor | soft/hard bands、dynamic pretrade risk reserve、hold/loss/deadline/stop 觸發、flatten/cooldown 決策 |
| ExecutionPort / AccountPort | 窄相容層重用成熟 execution proof／authenticated truth；單一 mutation authority、exact cancel/IOC terminal proof、unknown/uncertain fail closed |
| SessionLedger | 去重 fills、partial aggregation、全 session economics、time-weighted inventory、最終平倉與 fee 歸因 |
| Orchestrator | 取得快照 → governor constraints → quote/execution → ledger/telemetry；不承擔策略、帳本或交易細節 |
| Clock / TelemetrySink | 可測試的時間及 append-only JSONL 事件；不新增 campaign validator |

## 報價與 inventory

Reservation price 的 bps shift 為 `-clamp(position / hard_limit, -1, 1) * skew_bps_at_hard`。Long 向下偏移、short 向上偏移；bid 向下取 tick，ask 向上取 tick，且 `bid < external ask`、`ask > external bid`。

- `abs(position) < soft_limit`：持續雙邊。
- soft 至 hard：risk-increasing side 縮量／外移，低於 min lot 就省略，不回到 ping-pong。
- 到 hard：僅 reducing POST_ONLY at passive touch，不受 entry breakeven 價錨限制。
- 超過 hard：flatten。新 risk-increasing quote 必須先保留「可能成交量 × stop distance/loss + taker fee」的動態風險額，projected session loss 嚴格低於 cap。

Volatility 只暴露 multiplier；cadence、half-life、buffer cap 為內部 profile。起始版沒有 adverse-selection controller；Phase 10 只在 baseline 可行／接近可行時評估一個 markout EWMA buffer。

## 狀態與退出契約

策略狀態 `QUOTING / SKEWED / REDUCE_ONLY / FLATTENING / COOLDOWN / SESSION_COMPLETE` 與 execution health `HEALTHY / PAUSED_DATA / PAUSED_ORDER_STATE / HALTED` 分離；health 暫停不重置持倉 age 或 session budget。

```text
hold / loss / cap breach / deadline / operator stop / session loss
  → cancel all → exact zero-order proof → fresh authenticated position
  → optional passive grace → reducing LIMIT IOC (bounded price/attempt/deadline)
  → exact terminal proof → fresh authenticated residual → bounded retry
  → authenticated flat → cooldown 或 session complete
```

Cancel/fill race 以最新 authenticated position 為準；IOC partial 後只處理 residual。Uncertain terminal 狀態先 reconciliation，不盲重送。Session stop/deadline/loss 不得 cooldown 後重新進場。資料不可信、無法證明撤單或 bounded exit 未平倉時，保留真實失敗／殘倉並停止增加風險，不回報成功、不自動放寬限制。

## Ledger 與驗證

Fill idempotency、out-of-order fail closed、maker/taker turnover/fee 分離。保留 gross、funding/cashflow、all-in net/cost bps、fee cover、max drawdown、avg/P95 abs inventory、age、forced flatten count/loss、quote uptime、turnover/fills per quote hour；每筆 fill 可拆 spread capture、inventory markout、flatten concession、fee。Final-flat account reconciliation 未通過時，economic 結論不可用。

驗證以公開 port／行為為契約，不以 private state/reason strings 綁定 V1。依序完成純函式／ledger／execution／orchestrator 測試，再覆蓋八種 replay：calm、short 遇上漲、long 遇下跌、震盪 fills、stale、cancel/fill race、deadline nonflat、IOC partial residual。Phase 7 才做 10min dry smoke → 30min dry T3；此時只判定 safety/liveness simulation，economics 未評估。

Phase 8 的三組 target-edge canary 需要逐場新 live＋bounded-flatten 授權。Inventory tuning 在 Phase 8 可行後才開始；2h/4h/24h 依計畫順序，不沿用 V1 Gate A/B/C。V2 4h GO 前保留 V1 runtime/config/tests 和安全歷史；需要修改 shared Lighter adapter 時，必須先說明 Grid 影響並執行相關回歸。
