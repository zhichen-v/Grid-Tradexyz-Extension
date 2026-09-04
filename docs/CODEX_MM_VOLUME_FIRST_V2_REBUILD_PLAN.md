# Codex Task：Lighter Volume-First Market Maker V2 全面重整計畫

## 0. 文件地位

本文件是新的最高優先級重整任務，**取代並停止延伸**先前的：

- `CODEX_MM_POST_27ABAE_CHANGE_PIPELINE.md`
- 以 Gate A/B/C/D/F、toxicity calibration campaign、每筆 inventory episode 自然 flat 為核心的後續擴充
- 針對每次 live 事故逐項新增 guard、telemetry、schema、campaign validator 的開發方式

本任務不是繼續修補 `core/services/market_maker/`，而是：

> 保留已驗證的交易所連線、訂單 mutation safety 與 authenticated account truth；重新建立一套以「高 maker turnover、整體 fee cover、允許單筆虧損、持倉必須有 bounded-time exit」為目標的 Market Maker V2。

Repository 基準：

- Repository：`zhichen-v/Grid-Tradexyz-Extension`
- 原分支：`feat/lighter-market-maker-mvp`
- 規劃時遠端 HEAD：`59f313e7cc03c423877baa4b71e5467c67998e38`
- 最新程式基準 commit：`0a377987c74148534003b021da93de649f28720e`
- 最新文件 commit：`59f313e7cc03c423877baa4b71e5467c67998e38`
- 建議新分支：`refactor/lighter-volume-mm-v2`

除非使用者另行授權，Codex 不得執行 live、flatten、margin/leverage mutation、commit 或 push。

---

# 1. 為什麼必須重整，而不是繼續修眼前問題

## 1.1 原始產品目標

原始目標不是「每一筆 round trip 都獲利」，而是：

1. 在短時間內建立高 maker 交易量。
2. 盡可能以 maker spread capture 覆蓋 maker fee。
3. 單筆交易、單次 inventory cycle 可以虧損。
4. 整個固定時間 session 的 all-in 經濟結果才是主要判定。
5. 遇到單邊行情時必須有明確止損，不得無限等待，也不得只撤單留倉。
6. 不做 self-trade、wash trading 或不真實的成交量行為。

更精確的產品類型是：

> Fee-neutral volume market maker：在風險與最大損失限制下，最大化 maker turnover，而不是最大化每筆交易勝率或每個 episode 的正 PnL。

## 1.2 現行系統實際優化的目標

現行系統逐步演化成：

- 每次 flat entry 後停止同向增加倉位；
- 只保留相反方向 `POST_ONLY + reduce_only`；
- 用 fee-aware price 阻止單一 episode 認賠；
- 要求 authenticated natural flat；
- natural flat 前不開下一個 episode；
- 以 completed episode/fills 作為主要 promotion denominator；
- 對每次事故增加 guard、證據 schema、campaign validator 與 deterministic mirror。

這套設計優化的是：

```text
每一個 inventory episode 的安全、可歸因、自然 flat
```

不是：

```text
每小時 maker turnover，在 session-level fee cover 約束下最大化
```

兩者存在直接衝突。

## 1.3 最新 live 已經證明目標錯配

最近一次 Gate C live：

- 30 分鐘只有一筆 maker SELL entry；
- 建立 short `0.00020 BTC @ 78925.0`；
- 行情大幅上漲；
- `active_unwind_enabled:false`；
- `max_session_loss_for_maker_exit:0`；
- reduce-only BUY 被鎖在不虧損 economic boundary `78905.7`；
- 最終 `INVENTORY_HOLD`，無法 natural flat；
- runtime 停止後由操作者自行平倉；
- 相對 session baseline 的保守 flat-equity change 為 `-0.265309 USDG`。

這不是 order lifecycle bug，而是目前政策的必然結果：

```text
不允許單筆認賠
+ 不允許 taker stop
+ 要求 natural flat
= 單邊行情下停止產生 volume，最後仍需人工承擔損失
```

## 1.4 複雜度已超過策略本身

目前單一 `MarketMakerConfig` 已約有 76 個 user-visible fields。

主要 runtime 檔案已非常龐大：

- `coordinator.py` 約 119 KB
- `order_manager.py` 約 104 KB
- `account_monitor.py` 約 70 KB
- `inventory_unwind.py` 約 53 KB
- `metrics.py` 約 48 KB

兩個分析腳本：

- `analyze_market_maker_strategy.py` 約 62 KB
- `mm_calibration_campaign.py` 約 99 KB

最近 checkpoint 已達：

- Market Maker tests：649
- Full repository tests：896，另有既知 baseline failure/error

這些程式與測試不是毫無價值；它們證明了 mutation safety、account truth 與 evidence discipline。但它們已經成為策略迭代阻力。

## 1.5 重整的基本決策

1. **停止在 V1 增加新策略功能。**
2. V1 只接受 critical safety bugfix。
3. 另建 `market_maker_v2` package 與 runner。
4. V2 不追求 backward-compatible config。
5. 不把 V1 的 76 個 config fields、650 個測試與 160 KB campaign/analyzer 原樣搬過去。
6. V2 先證明經濟可行，再增加進階 telemetry。
7. 每一個新增設定必須直接對應：
   - volume；
   - spread/fee economics；
   - inventory；
   - stop-loss；
   - session risk；
   其中一項。否則不得成為 user-facing config。

---

# 2. 先承認的經濟邊界

## 2.1 Fee floor

目前 authenticated maker fee 為每側約 `1.2 bps`。

一個完整 maker buy + maker sell round trip，僅 fee 即約：

```text
2 × 1.2 bps = 2.4 bps
```

因此，在忽略 adverse selection 的理想情況下，完整 buy/sell spread capture 至少需要大於 `2.4 bps` 才能 fee-neutral。

若希望 full-roundtrip session edge 為 `E bps`，對稱報價的最低單側距離約為：

```text
minimum_half_spread_bps
=
maker_fee_bps
+
E / 2
+
adverse_selection_buffer_bps
```

這是一個市場經濟問題，不是程式問題。

如果 Lighter BTC 在可獲得高 fill rate 的價位，實際可捕捉 spread 小於 roundtrip fee 加 adverse selection，則：

> 單市場、高 volume、純交易 PnL fee cover 三者可能無法同時成立。

V2 必須快速建立 volume/fee Pareto curve。若不存在可行點，應停止繼續添加策略參數，改評估：

- 更低 fee tier 或 maker rebate；
- 將交易所 points/incentives 明確換算成 bps；
- 第二市場 hedge；
- 其他 symbol；
- 接受可量化的 volume acquisition cost。

## 2.2 新目標函數

V2 的第一目標：

```text
maximize maker_turnover_per_quote_hour
```

受以下硬限制約束：

```text
all_in_net_cost_bps <= configured cost budget
max_drawdown <= session risk limit
abs(position) <= hard inventory limit
final position/orders = 0/0
hard safety incidents = 0
```

若目標是完整 fee cover：

```text
all_in_net_cost_bps <= 0
fee_cover_ratio >= 1
```

其中 all-in 必須包含：

- maker fees；
- taker flatten fees；
- realized inventory PnL；
- session 結束 flatten；
- stop-loss；
- 不可歸因的 funding/cashflow 需單獨揭露。

## 2.3 不再使用的主要判定

V2 不再把以下條件作為策略核心：

- 每筆交易必須盈利；
- 每個 episode 必須覆蓋自己的 fee；
- 每次一有持倉就停止所有正常 market making；
- 必須 natural flat 才能繼續；
- 30 completed maker fills 才能知道策略是否有效；
- non-flat 本身等同軟體錯誤；
- `max_session_loss_for_maker_exit=0` 作為 production 預設。

---

# 3. Hummingbot 參考方式

只借用 Hummingbot 的結構，不直接搬程式碼或參數。

## 3.1 應借用

Hummingbot V2 的 `MarketMakingControllerBase` 將：

- buy/sell spreads；
- buy/sell amount allocation；
- reference price；
- spread multiplier；
- executor refresh；
- cooldown；
- position/stop management；

分開處理。

`PMMSimpleController` 本身很薄，只將 level、price、amount 轉成 executor config。

V2 應借用這個原則：

```text
Market Data
→ thin Quote Controller
→ generic Execution Port
→ separate Inventory/Risk Governor
→ Session Recorder
```

## 3.2 不應搬入

不得直接搬入：

- Hummingbot connector mutation model；
- Hummingbot market order stop 實作；
- 完整 PositionExecutor；
- 全套 MACD/NATR；
- 全套 Controller configuration surface；
- Hummingbot internal recorder database。

## 3.3 Stop-loss 的必要結論

Hummingbot 的 PositionExecutor 對 stop-loss/time-limit 使用 MARKET order。

V2 對 Lighter 採較嚴格替代：

```text
cancel all managed quotes
→ fresh authenticated position/BBO
→ bounded reduce-only LIMIT + IOC
→ exact terminal proof
```

但 live volume mode 必須明確授權這條 terminal path。若禁止 active flatten，就不得宣稱可 unattended long-run。

---

# 4. Repository 與分支策略

## 4.1 凍結 V1

在最新 commit 建立可追溯 tag，例如：

```text
mm-v1-guard-driven-20260903
```

保留：

```text
run_market_maker.py
core/services/market_maker/
config/market_maker/
scripts/analyze_market_maker_strategy.py
scripts/mm_calibration_campaign.py
docs/market_maker_*
tests/test_market_maker_*
```

V1 狀態：

```text
legacy / safety reference / no new strategy feature
```

只允許：

- credential leakage；
- unauthorized mutation；
- position/order ownership；
- exact terminal proof；
- exchange contract；
- shutdown cleanup；

相關 critical 修復。

## 4.2 新建 V2

新增：

```text
run_volume_market_maker.py

core/services/market_maker_v2/
  __init__.py
  config.py
  domain.py
  market_state.py
  quote_policy.py
  inventory_governor.py
  session_ledger.py
  execution_port.py
  orchestrator.py
  telemetry.py

config/market_maker_v2/
  lighter_btc_volume.example.yaml

scripts/
  analyze_mm_v2_session.py

docs/mm_v2/
  OBJECTIVE.md
  ARCHITECTURE.md
  OPERATING_GUIDE.md
  EXPERIMENT_LOG.md

tests/
  test_mm_v2_config.py
  test_mm_v2_quote_policy.py
  test_mm_v2_inventory_governor.py
  test_mm_v2_session_ledger.py
  test_mm_v2_execution_port.py
  test_mm_v2_orchestrator.py
  test_mm_v2_replay_scenarios.py
```

## 4.3 不要在原 package 內繼續重構

不要直接：

- 刪改 `MarketMakerConfig`；
- 將 76 fields 一次改成 nested config；
- 重寫 119 KB `coordinator.py`；
- 在 104 KB `order_manager.py` 中增加 V2 分支；
- 讓所有 V1 tests 同時適配 V2。

這會再次讓工作被 backward compatibility 與 test updates 淹沒。

---

# 5. V2 目標架構

```text
                         ┌──────────────────────┐
Lighter WS / REST ──────▶│ MarketState          │
                         │ external BBO          │
                         │ mid / microprice      │
                         │ short EWMA volatility │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ InventoryGovernor    │
                         │ position band        │
                         │ skew / stop / flatten│
                         └──────────┬───────────┘
                                    │ constraints
                                    ▼
                         ┌──────────────────────┐
                         │ VolumeQuotePolicy    │
                         │ fee floor            │
                         │ target edge          │
                         │ inventory skew       │
                         │ volatility buffer    │
                         └──────────┬───────────┘
                                    │ QuotePlan
                                    ▼
                         ┌──────────────────────┐
                         │ ExecutionPort        │
                         │ cancel-confirm-create│
                         │ bounded IOC flatten  │
                         └──────────┬───────────┘
                                    │
                                    ▼
                                  Lighter

Authenticated account/fills ──────▶ SessionLedger
                                          │
                                          ▼
                                    Telemetry/Report
```

## 5.1 Orchestrator 只做協調

每個 cycle：

```python
market = market_state.snapshot()
account = account_port.snapshot()
risk = inventory_governor.evaluate(market, account, session_ledger)

if risk.action == HALT:
    execution.cancel_all()
elif risk.action == FLATTEN:
    execution.flatten(risk.flatten_intent)
else:
    plan = quote_policy.propose(market, account, risk)
    execution.reconcile(plan)

session_ledger.update(...)
```

Orchestrator 不負責：

- 計算 stop boundary；
- 計算 fee floor；
- 決定 strategy parameter；
- 產生 campaign evidence；
- 判斷每個 episode 是否應盈利。

## 5.2 兩個正交狀態

不要再用一個 `RuntimeState` 混合策略與系統錯誤。

### Execution health

```text
HEALTHY
PAUSED_DATA
PAUSED_ORDER_STATE
HALTED
```

### Strategy state

```text
QUOTING
SKEWED
REDUCE_ONLY
FLATTENING
COOLDOWN
SESSION_COMPLETE
```

Execution health 永遠可以覆蓋 Strategy state。

---

# 6. VolumeQuotePolicy V1 規格

## 6.1 Reference price

```text
reference =
microprice when valid
else external mid
```

External book 必須排除可明確識別的 own order size。

初版不加入：

- MACD；
- NATR；
- trend_guard；
- toxicity block confirmations；
- online markout feedback；
- machine learning。

## 6.2 Fee-aware spread

令：

```text
f = authenticated maker fee in bps per side
e = configured target full-roundtrip net edge in bps
v = short-horizon volatility buffer in bps
```

初版對稱 half spread：

```text
half_spread_bps = f + e / 2 + v
```

可再加 inventory side adjustment，但不能低於 fee floor：

```text
bid_half_spread >= f
ask_half_spread >= f
```

價格：

```text
bid = floor_to_tick(reservation * (1 - bid_half_spread_bps / 10000))
ask = ceil_to_tick(reservation * (1 + ask_half_spread_bps / 10000))
```

並 clamp：

```text
bid < external_best_ask
ask > external_best_bid
```

## 6.3 Volatility buffer

只使用一個短期 EWMA realized volatility。

建議：

```text
v = min(max_volatility_buffer, volatility_multiplier × ewma_move_bps)
```

初版 profile 固定內部：

- EWMA half-life；
- sample cadence；
- maximum buffer。

YAML 只暴露 `volatility_multiplier`，不得暴露 5/15/60 秒各自門檻。

## 6.4 Inventory reservation price

令：

```text
inventory_ratio = position / hard_inventory_limit
```

clamp 至 `[-1, 1]`。

```text
reservation_shift_bps
=
-inventory_ratio × skew_bps_at_hard_limit
```

```text
reservation =
reference × (1 + reservation_shift_bps / 10000)
```

效果：

- long：reservation 下移，ask 變近、bid 變遠；
- short：reservation 上移，bid 變近、ask 變遠。

## 6.5 Continuous quoting

在 `abs(position) < soft_limit`：

- 保持雙邊；
- 以 reservation shift 控制 inventory；
- risk-increasing side 可減少 size 或增大 spread；
- inventory-reducing side可較積極。

在 `soft_limit <= abs(position) < hard_limit`：

- 仍可保留雙邊，但風險增加側至少明顯縮小或外移；
- 若 minimum lot 無法縮小，允許直接停掉風險增加側；
- 不要求先回到 flat 才恢復。

在 `abs(position) >= hard_limit`：

- 只保留 inventory-reducing `POST_ONLY + reduce_only`；
- 進入 `REDUCE_ONLY`。

移除 V2 的 `ping_pong_enabled`。上述 behavior 是固定策略規格，不是可選開關。

## 6.6 Order levels

V2 第一版只支援一層 bid + 一層 ask。

原因：

- 先建立 volume/fee curve；
- 避免同時引入多層、queue allocation、size allocation；
- 單層 economics GO 後才增加第二層。

第二層屬後續獨立 experiment，不是 V2 MVP。

---

# 7. InventoryGovernor 與止損

## 7.1 核心原則

單筆或單次 inventory cycle 可以虧損。

禁止再使用：

```text
每個 episode 不得認賠
```

改為：

```text
整個 session 的 all-in net/cost 與 drawdown受限
```

## 7.2 Strategy states

### QUOTING

- flat 或 inventory低；
- 正常雙邊 quote。

### SKEWED

- inventory進入 soft band；
- price/size skew；
- 仍允許 market making。

### REDUCE_ONLY

- inventory達 hard band；
- 移除 risk-increasing side；
- 在 best passive touch附近掛 reduce-only maker；
- 不使用 entry price 作為不可突破的 break-even anchor。

### FLATTENING

任一條件成立：

```text
inventory_age >= max_hold_seconds
unrealized_inventory_loss >= stop_loss_usdg
abs(position) > hard_limit
session deadline reached
operator stop requested
```

流程：

1. cancel all managed quotes；
2. exact terminal proof；
3. fresh authenticated position；
4. 可選 passive grace；
5. bounded reduce-only LIMIT + IOC；
6. exact terminal proof；
7. partial residual重新 authenticated 後 bounded retry；
8. flat 後進入 cooldown。

### COOLDOWN

- forced flatten 後短時間不報價；
- 讓行情與 data state穩定；
- 到期後重新 QUOTING。

### SESSION_COMPLETE

- cancel；
- flatten；
- authenticated position/orders `0/0`；
- 輸出 final economics；
- 正常停止。

## 7.3 Live volume mode 的硬規則

Live volume mode 若未明確授權 bounded flatten，startup 必須拒絕。

建議 CLI：

```text
--authorize-bounded-flatten
```

這是每次 live session 的顯式授權，不存入 committed YAML。

不得再出現：

```text
active_unwind_enabled:false
但又宣稱可 unattended 30m/4h/24h
```

## 7.4 Pre-trade risk

不再預留固定 `0.075 USDG` episode cap。

對目前 position + open orders 計算：

```text
worst_case_position
worst_case_stop_loss
worst_case_taker_fee
projected_session_loss
```

只有：

```text
projected_session_loss < session_loss_limit
```

才可保留 risk-increasing quote。

Reserve 應由：

```text
potential filled quantity
× configured stop distance/loss
+ expected taker fee
```

動態計算，不使用固定 episode reserve。

## 7.5 Stop-loss 不是獲利邏輯

Stop-loss 的工作是：

- 保持 bounded time to flat；
- 限制 tail loss；
- 讓 high-volume loop可以恢復。

Stop-loss 不負責 fee cover。

若 forced flatten發生頻率太高，代表：

- spread太窄；
- inventory buffer太小；
- skew不足；
-市場不適合；

而不是應該放大 session loss cap。

---

# 8. SessionLedger：以 session 為中心

## 8.1 不再以 episode 為主要 promotion unit

仍可記錄 flat-crossing inventory cycle供分析，但它不能：

- 阻止 quote；
- 控制是否可開下一筆；
- 要求每個 cycle正收益；
- 成為主要 promotion denominator。

## 8.2 必須保存的經濟項目

```text
maker_buy_turnover
maker_sell_turnover
maker_turnover_total
taker_flatten_turnover
realized_gross_pnl
maker_fee
taker_fee
funding_or_cashflow
all_in_net_pnl
all_in_net_cost_bps
fee_cover_ratio
max_drawdown
average_abs_inventory
p95_abs_inventory
inventory_age
forced_flatten_count
forced_flatten_loss
quote_uptime_seconds
maker_turnover_per_quote_hour
fills_per_quote_hour
```

## 8.3 Gross decomposition

每個 fill至少分解：

```text
spread_capture_at_fill
inventory_markout
flatten_concession
fee
```

Session report輸出：

```text
spread capture
inventory drift
forced flatten cost
fees
net
```

不要為這些資料建立 100 KB campaign validator。使用簡單、append-only JSONL。

## 8.4 最終判定

固定時間 session 結束時：

1. 停止新 quote；
2. cancel；
3. bounded flatten；
4. authenticated flat；
5. 將 flatten成本納入；
6. 輸出 final report。

只有完整 final report可用於 economics。

---

# 9. Minimal V2 Config

## 9.1 原則

- user-facing fields 目標不超過 18。
- authenticated fee不由 YAML 指定。
- safety invariants不由 YAML 關閉。
- validation thresholds不放在 strategy config。
- telemetry格式不放在 strategy config。
- no backward compatibility。

## 9.2 建議 schema

```yaml
market_maker_v2:
  symbol: "BTC"
  profile: "fee_neutral_volume_v1"
  dry_run: true

  quote:
    order_size: "0.00020"
    target_net_edge_bps: "0.20"
    volatility_multiplier: "1.0"
    reprice_threshold_ticks: 5
    max_quote_age_ms: 5000

  inventory:
    soft_limit: "0.00020"
    hard_limit: "0.00040"
    skew_bps_at_hard: "2.0"

  flatten:
    max_hold_seconds: 180
    stop_loss_usdg: "0.05"
    passive_grace_seconds: 10
    ioc_slippage_ticks: 2

  session:
    duration_seconds: 1800
    max_loss_usdg: "0.20"
    cooldown_seconds: 30
```

以上值只是 schema 示例，不是 live 推薦值。

## 9.3 不再出現於 V2 YAML

移除：

```text
quote_mode
ping_pong_enabled
post_only
exclusive_symbol_control
startup_open_order_policy
unknown_order_policy
cancel_on_shutdown
maker_fee_rate
taker_fee_rate
account_audit_interval_seconds
account_audit_timeout_seconds
stale_book_seconds
stale_position_seconds
position_poll_interval_seconds
order_sync_interval_seconds
health_check_interval_seconds
max_consecutive_errors
error_cooldown_seconds
max_mutations_per_minute
trend_guard_*
toxicity_*
soft_position_ratio
hard_position_ratio
soft_exit_*
max_session_loss_for_maker_exit
active_unwind_enabled
active_unwind_after_seconds
active_unwind_loss_trigger
active_unwind_max_attempts
active_unwind_confirmation_timeout_seconds
max_episode_loss_for_unwind
max_session_loss_for_unwind
economic_min_fills
min_completed_net_turnover_bps
require_flat_start
log_status_interval_seconds
```

其中 safety/timing 使用 code-owned `LighterVolumeRuntimeProfile`，不是消失：

```python
LIGHTER_VOLUME_RUNTIME_PROFILE = {
    "post_only_quotes": True,
    "exclusive_symbol_control": True,
    "require_flat_start": True,
    "cancel_on_shutdown": True,
    "unknown_order_policy": "pause",
    "account_audit_interval_seconds": 10,
    "stale_book_seconds": 3,
    "stale_position_seconds": 10,
    "max_flatten_attempts": 3,
}
```

---

# 10. ExecutionPort：重用安全，不繼續污染策略

## 10.1 初版使用 wrapper

不要立刻重寫現有 mutation lifecycle。

新增窄介面：

```python
class ExecutionPort(Protocol):
    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult: ...
    async def cancel_all_managed(self) -> ExecutionResult: ...
    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult: ...
    def snapshot(self) -> ExecutionSnapshot: ...
```

第一版可由 current `MarketMakerOrderManager` 的 compatibility wrapper實作。

## 10.2 凍結現有 OrderManager

V2 strategy 不得直接：

- 存取 slot private fields；
- 解析 current reason string；
- 新增 controller-specific branch；
- 新增 campaign telemetry；
- 新增 V2 config dependency。

若 compatibility wrapper無法完成，先寫 adapter layer，不要改 104 KB file。

## 10.3 後續抽取

只有 V2 economics與4h liveness已成立後，才另案抽取：

```text
LighterSafeOrderEngine
```

抽取前不得重寫已 live-proven exact terminal semantics。

---

# 11. Testing Strategy 重整

## 11.1 不搬移 649 個 V1 tests

V1 suite保持原樣，作為 legacy regression。

V2 own suite目標：

```text
約 80–120 個 public-contract tests
```

不是硬性數字，但超過前必須檢查是否又在測 private implementation details。

## 11.2 測試金字塔

### Fast pure tests

- fee floor；
- quote price；
- inventory skew；
- state transitions；
- stop trigger；
- session ledger；
- config validation。

目標：數秒完成。

### Execution contract tests

- cancel-confirm-create；
- unknown/uncertain；
- partial IOC；
- final flat；
- no self-trade；
- no risk-increasing order in hard band。

重用現有 proven mocks/fixtures，不複製全部 V1 branch cases。

### Replay scenario tests

只建立少量高價值情境：

1. calm mean-reverting；
2. short後單邊上漲；
3. long後單邊下跌；
4. oscillating high fill；
5. stale book；
6. cancel/fill race；
7. session deadline non-flat；
8. IOC partial residual。

每個 scenario驗證最終 business behavior，不驗證每個 private counter。

## 11.3 CI 分層

```text
mm_v2_fast
mm_v2_execution
mm_v2_replay
legacy_mm_regression
full_repo
```

日常迭代：

```text
先 fast
→ affected execution/replay
→ milestone 時才全 MM/full repo
```

不得每改一個策略公式就重寫大量 legacy tests。

## 11.4 Complexity budget

Soft limits：

- config user fields <= 18
- `orchestrator.py` <= 500 LOC
- `quote_policy.py` <= 350 LOC
- `inventory_governor.py` <= 400 LOC
- `analyze_mm_v2_session.py` <= 500 LOC
- 單一函式盡量 <= 60 LOC
- 不新增第二個 campaign analyzer
- 每增加一個 config field必須在 PR 中說明其 objective/risk owner

超過 soft limit需在 commit summary說明，不能無聲擴張。

---

# 12. 完整改動 Pipeline

## Phase 0：Freeze 與重整契約

### 工作

1. 建立 V1 tag。
2. 新建 `refactor/lighter-volume-mm-v2`。
3. 新增：
   - `docs/mm_v2/OBJECTIVE.md`
   - `docs/mm_v2/ARCHITECTURE.md`
4. 在 V1 docs頂部標示 legacy/frozen。
5. 在新文件明確列出：
   - objective；
   - non-goals；
   - fee floor；
   - stop authorization；
   - complexity budget。

### 禁止

- 不改 runtime。
- 不跑 live。
- 不搬舊 config。

### Commit

```text
docs(mm-v2): define volume-first product and freeze legacy runtime
```

---

## Phase 1：Economic Feasibility Tool

### 工作

新增小型 read-only：

```text
scripts/mm_v2_feasibility.py
```

輸入：

- existing orderbook/market logs，或
- dry-run captured BBO snapshots。

輸出：

```text
authenticated maker/taker fee
roundtrip fee floor
BBO spread distribution
1s/5s realized move distribution
candidate target-edge spreads
minimum required full spread
estimated quote distance in ticks
```

明確標示：

```text
not a queue-fill backtest
```

### 目的

在 live 前回答：

```text
要 cover 2.4 bps roundtrip fee，合理 quote大約要多遠？
該距離是否可能有足夠 touch frequency？
```

### 限制

- script <= 300–500 LOC；
- 無 campaign manifest；
- 無 Git/worktree identity authority；
- 無 account mutation。

### Commit

```text
feat(mm-v2): add fee and spread feasibility report
```

---

## Phase 2：V2 Skeleton 與 Safety Ports

### 工作

新增 package、domain models與 ports：

```text
MarketStateSnapshot
AccountSnapshot
QuoteIntent
QuotePlan
FlattenIntent
InventoryDecision
ExecutionResult
SessionReport
```

建立：

```text
MarketDataPort
AccountPort
ExecutionPort
Clock
TelemetrySink
```

新增 current Adapter/OrderManager compatibility wrapper。

### 驗收

- 無 strategy behavior；
- dry synthetic cycle可以輸出空 QuotePlan；
- V1 untouched；
- no credentials in models/logs。

### Commit

```text
feat(mm-v2): add isolated runtime and safety ports
```

---

## Phase 3：SessionLedger

### 工作

實作：

- fill ingestion；
- maker/taker separation；
- realized gross；
- fee；
- net；
- turnover；
- drawdown；
- inventory duration；
- final flatten cost；
- one JSONL event stream；
- final report。

### 驗收

- duplicate fill idempotent；
- out-of-order fail closed；
- partial fill aggregation；
- all-in net exact；
- final report沒有 natural-flat survivorship bias。

### Commit

```text
feat(mm-v2): add session-level economics ledger
```

---

## Phase 4：VolumeQuotePolicy

### 工作

實作：

- external BBO；
- microprice fallback；
- authenticated fee floor；
- target edge；
- EWMA volatility buffer；
- inventory reservation shift；
- one bid + one ask；
- one-layer only；
- post-only boundary；
- no self-cross。

### 測試

- calm；
- fee change；
- short/long skew；
- extreme vol；
- invalid data；
- tick rounding。

### Commit

```text
feat(mm-v2): add fee-aware continuous quote policy
```

---

## Phase 5：InventoryGovernor 與 Bounded Flatten

### 工作

實作：

```text
QUOTING
SKEWED
REDUCE_ONLY
FLATTENING
COOLDOWN
SESSION_COMPLETE
```

觸發：

- soft/hard inventory；
- max hold；
- stop loss；
- session deadline；
- operator stop；
- session loss。

接上既有 bounded IOC safe lane。

### 必測 scenario

short後價格大幅上漲：

```text
SELL fill
→ reservation上移
→ ask風險增加被外移/關閉
→ bid接近passive touch
→ age/loss trigger
→ cancel terminal
→ bounded IOC BUY
→ authenticated flat
→ cooldown
```

不得出現：

```text
hold在break-even price直到session結束
```

### Commit

```text
feat(mm-v2): add inventory bands and bounded-time flatten
```

---

## Phase 6：Minimal Config 與 Runner

### 工作

新增 `MarketMakerV2Config` nested schema與 runner。

Runner要求：

- flat start；
- no open orders；
- authenticated fee；
- exclusive symbol；
- dry-run default；
- live需 `--authorize-bounded-flatten`；
- fixed duration；
- final cancel + flatten + postflight。

### 驗收

- config fields <= 18；
- example dry-run；
- live authorization缺失時在 connect/mutation 前拒絕；
- session end一定走 flatten流程；
- final report包含全部成本。

### Commit

```text
feat(mm-v2): add minimal config and bounded-session runner
```

---

## Phase 7：Replay 與 Dry Validation

### 工作

1. Scenario replay。
2. 10 分鐘 dry smoke。
3. 30 分鐘 dry T3。
4. 觀察：
   - quote uptime；
   - quote revisions；
   - inventory state；
   - no real mutation；
   - resource stability。

### Gate

```text
safety = GO
liveness simulation = GO
economics = not evaluated
```

不建立 elaborate Gate A/B/C campaign。

### Commit

```text
test(mm-v2): validate replay and dry runtime contracts
```

---

## Phase 8：Economic Canary Matrix

### 原則

不要先加 toxicity。

只測一個最核心 economic knob：

```text
target_net_edge_bps
```

候選例如：

```text
narrow
balanced
wide
```

實際數值由 Phase 1 fee/spread report決定，不在程式中硬寫。

### 每個 live canary

- 10–30 分鐘；
- 同 order size；
- 同 inventory/risk；
- bounded flatten已授權；
- session結束強制 flat；
- 全部 flatten成本納入。

### 產出 Pareto

每個 candidate：

```text
maker turnover/hour
all-in net cost bps
fee cover
quote uptime
average/P95 inventory
forced flatten rate
max drawdown
```

選擇：

```text
在 net cost約束下 turnover最高
```

不是：

```text
單次 PnL最高
```

### Kill criterion

若三個 spread candidate都無法同時：

```text
fee cover >= 1
且 maker turnover明顯高於V1
```

則標記：

```text
single_venue_fee_neutral_volume_infeasible
```

停止加策略參數，改評估 fee tier、incentive、symbol或 hedge。

---

## Phase 9：Inventory Parameter Matrix

只有 Phase 8 存在可行 spread點後才測：

1. `soft_limit`
2. `hard_limit`
3. `skew_bps_at_hard`

一次只改一個。

目的：

- 增加 continuous quoting time；
- 降低 forced flatten；
- 不增加 tail loss。

不得同場改：

- target edge；
- order size；
- stop loss；
- inventory limits。

---

## Phase 10：進階 Adverse Selection Filter

只有 baseline VolumeQuotePolicy 已 fee-neutral或接近時才加入。

第一版只允許一個內部 signal：

```text
short-horizon side-specific adverse markout EWMA
```

它只影響：

```text
side spread buffer
```

不加入：

- 20 個 toxicity config；
- confirmations matrix；
- campaign profile ID；
- MACD/NATR；
- blocking/resume state machine。

需要 blocking時，作為後續獨立策略版本：

```text
fee_neutral_volume_v2_blocking
```

不是在同一 config繼續堆欄位。

---

## Phase 11：2h、4h、24h

### 2 小時

要求：

- 3 次 short canary聚合存在可行點；
- final flat；
- safety zero；
- all-in economics完整；
- forced flatten在可接受範圍。

### 4 小時

主要判定：

```text
maker_turnover_per_hour
aggregate fee cover
all-in net cost bps
max drawdown
forced flatten loss share
```

### 24 小時

另行處理：

- persistent ledger；
- bounded order registry；
- restart recovery-only mode；
- watchdog；
- funding；
- memory soak。

不得在 30 分鐘策略尚未有經濟可行點時，先建 24h evidence infrastructure。

---

## Phase 12：V1 Decommission

只有 V2 完成 4h GO後：

1. README將 V2 設為推薦。
2. V1 runner標示 deprecated。
3. V1 docs移入 `docs/archive/mm_v1/`。
4. 舊 calibration scripts不再為 CI required。
5. 24h GO後再決定是否刪除 V1 runtime。

---

# 13. 新 Promotion Contract

## 13.1 Safety Gate

必須：

```text
unknown order = 0
unresolved mutation = 0
reconciliation failure = 0
self trade = 0
position cap breach = 0
final open orders = 0
final position = 0
```

## 13.2 Liveness Gate

```text
stop/deadline後在flatten deadline內authenticated flat
```

自然 flat不是必要條件。

## 13.3 Volume Gate

使用：

```text
maker_turnover_per_quote_hour
```

輔助：

```text
maker_fills_per_quote_hour
quote_uptime
```

不以 completed episode數作主要 volume gate。

## 13.4 Economic Gate

```text
all_in_net_pnl
all_in_net_cost_bps
fee_cover_ratio
```

全部包含 final flatten。

短 canary用聚合判定，不要求每一場或每個 trade都盈利。

## 13.5 Risk Gate

```text
max_drawdown <= session.max_loss_usdg
abs(position) <= hard_limit
forced flatten不超過slippage/attempt boundary
```

---

# 14. Codex 明確禁止事項

1. 不在 V1 `MarketMakerConfig` 再加欄位。
2. 不增加新的 `soft_exit_*` 或 `toxicity_*` 參數。
3. 不把「某次 live 發生甚麼」直接轉成新 guard。
4. 不要求每個 episode正 PnL。
5. 不要求 natural flat後才繼續報價。
6. 不把 active flatten維持為 production optional。
7. 不把策略判定塞入 OrderManager。
8. 不新增第二個 50–100 KB analyzer/campaign script。
9. 不為 telemetry缺失停止安全正常的交易；telemetry與 risk truth需分級。
10. 不用 reason string承載 machine-critical intent。
11. 不為通過測試修改 unrelated Grid baseline。
12. 不自動執行 live、flatten、commit、push。
13. 不做 self-trade/wash-volume。
14. 不在沒有 Phase 8 feasibility結果前加入多層 quote。
15. 不在沒有單市場可行點前加入 machine learning。

---

# 15. Codex 每個 Phase 的報告格式

每個 phase完成時，只需回報：

```text
Branch
Commit or worktree state
Files changed
Public config field count
Runtime LOC/size change
Focused tests
V2 tests
Legacy MM regression
Full repo baseline（只在 milestone）
Behavioral acceptance results
Known blockers
Live/account mutation performed: yes/no
```

不要把完整測試輸出與所有歷史事故複製進主文件。

`docs/mm_v2/EXPERIMENT_LOG.md` 每次只增加一列：

```text
date | commit | config hash | duration | turnover/h | fee cover | net cost bps | DD | forced flatten | result
```

---

# 16. 建議 Commit 序列

```text
docs(mm-v2): define volume-first product and freeze legacy runtime

feat(mm-v2): add fee and spread feasibility report

feat(mm-v2): add isolated runtime and safety ports

feat(mm-v2): add session-level economics ledger

feat(mm-v2): add fee-aware continuous quote policy

feat(mm-v2): add inventory bands and bounded-time flatten

feat(mm-v2): add minimal config and bounded-session runner

test(mm-v2): validate replay and dry runtime contracts

docs(mm-v2): record economic canary matrix

feat(mm-v2): add calibrated inventory skew profile

feat(mm-v2): add optional adverse-selection spread buffer

docs(mm-v2): promote validated volume-first profile
```

每個 commit 必須可獨立 review；不得做一個數千行、數百測試的單一大 commit。

---

# 17. Definition of Done

V2 MVP 完成條件：

1. V1 已 frozen，沒有繼續膨脹。
2. V2 user-facing config <= 18 fields。
3. Live volume session必須顯式授權 bounded flatten。
4. 一筆 short 遇到持續上漲時：
   - 先 inventory skew；
   - 再 reduce-only；
   - 到 loss/time boundary後 IOC；
   - bounded-time authenticated flat；
   - 不再「無所作為」。
5. 單筆虧損被允許並完整計入 session。
6. Session結束不依賴 natural flat。
7. 最終 report包含 maker/taker fee與 flatten成本。
8. 至少建立三個 spread candidate的 volume/cost Pareto curve。
9. 找到至少一個：
   - fee cover >= 1；
   - 或明確的最低 acquisition cost bps；
   - 且 maker turnover顯著高於 V1；
   的 candidate。
10. 若找不到，正式得出 single-venue economic infeasibility，而不是繼續增加 guard。
11. V2 strategy tests維持 public-contract導向，沒有複製 V1 的測試爆炸。
12. 4h GO 前不建 24h campaign/evidence infrastructure。

---

# 18. 最終設計判斷

目前最重要的改變不是新指標、更多 guard或更精確的 shadow counterfactual，而是改變策略的基本單位：

```text
V1：
一個 fill
→ 一個 episode
→ 必須自然 flat
→ 才能繼續

V2：
連續雙邊 maker quoting
→ inventory在band內浮動
→ session-level economics
→ loss/time時bounded flatten
→ cooldown後繼續
```

這才符合「短時間高交易量、整體盡量 cover fee、單筆可以虧損」的原始方向。
