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

## 0.1 使用者覆寫：只保留 V2（2026-09-05）

使用者已明確要求完整移除 V1 Market Maker，包含其程式、設定、腳本、測試與文件，工作樹只保留 V2。這項指示取代原先「凍結保留 V1、4h 後才退役、24h 後再決定刪除」的限制；不建立另一份 V1 archive。

- 必要的已驗證訂單安全語義移入 V2 自有 `order_manager.py`／`execution_models.py`，透過 V2 ports 使用；不得繼續 import 舊 package。
- 不搬回舊 controller、toxicity、episode、intent-attribution 或 campaign 政策；只承接目前 V2 所需的 execution safety 與相關測試。
- 保留本計畫與 `docs/mm_v2/`，既有 Phase/run 證據不改寫；下文原 Phase 0–6 的凍結／V1 untouched 敘述只代表當時的歷史驗收。
- V1 歷史可由既有 Git commit/tag 追溯；目前執行契約只看 V2。Grid 不在移除範圍。
- 刪除 V1 不代表 V2 已達 production、economic 或 live GO，不改變既有 risk gate 與逐場授權要求。

## 0.2 重新審查與後續執行順序（2026-09-05）

本次依使用者要求重新 review Phase 0–7 並優化計畫。Review 基準為 `260be69`；首次 review 僅更新文件。使用者後續已授權按 R1→R5 開始修復，目前 worktree 已修 R1／R2 的程式缺口，R3 資料／預算與 T3 尚未通過；實作進度見 §19.4。歷史 run 結果維持原樣。

- 保留 continuous maker quoting、session-level fee cover、允許單筆虧損及 bounded exit 的產品方向，不再全面重寫 V2。
- 主目標改為固定完整時間窗的真實 maker turnover；quote-hour efficiency 改作診斷，避免把 cooldown、故障及撤換單的時間成本排除。
- Phase 7 尚未完成；先修已重現的 cleanup／quote lifecycle 缺口，再處理 clock、account/book coherence 與完整 live 讀取預算，不能只修時鐘後重跑 T3。
- 三個 spread candidates 是初步篩選，不能證明整個單市場策略不可能 fee-neutral。證據不足與「已測條件下未找到可行點」分開回報。
- 使用者提供測試帳戶約 **299 USDG**，尚未設定成交量目標。這是資金背景，非本輪 authenticated balance，也不是可損失額度；不據此提高 size、inventory、leverage 或 loss cap。
- 本輪無 live／帳戶 mutation／commit／push 授權。修復不需要重新 rebuild Grid，也不把提高帳戶 tier 當成預設解法。

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
2. 依 §0.1，V1 只保留於 Git 歷史，不再作為工作樹中的維護或執行目標。
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
maximize maker_turnover_per_wall_hour
```

受以下硬限制約束：

```text
all_in_net_cost_bps <= configured cost budget
max_drawdown <= session risk limit
abs(position) <= hard inventory limit
final position/orders = 0/0
hard safety incidents = 0
```

`maker_turnover_per_wall_hour = maker_turnover_total × 3600 / evaluation_seconds`。
每個比較窗口在開始前固定時長；包含 startup、quote replacement、單邊／減倉、cooldown、pause 及收尾。提早停止仍保留整個預定窗口，收尾超時則延長分母至实际收尾；不得挑出有掛單的秒數或刪去失敗窗口。`maker_turnover_per_quote_hour` 保留作有單時的條件效率，另報雙邊 working uptime 及 capital turnover，不代替主目標。實作與最小 analyzer 待 §19 R4。

若目標是完整 fee cover：

```text
all_in_net_cost_bps <= 0
fee_cover_ratio >= 1  # fees > 0 時；零費用以 net/cost 判定
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

## 4.1 移除 V1，只保留 Git 歷史

原凍結 tag 保持可追溯：

```text
mm-v1-guard-driven-20260903
```

依 §0.1 從工作樹移除以下 V1 範圍，不建立相容入口或 archive：

```text
run_market_maker.py
core/services/market_maker/
config/market_maker/
scripts/analyze_market_maker_strategy.py
scripts/mm_calibration_campaign.py
docs/market_maker_*
docs/CODEX_MARKET_MAKER_MVP_PIPELINE.md
tests/test_market_maker_*
```

必要的 ownership、mutation、exact terminal proof 與 bounded IOC 安全語義由 V2 execution modules 承接；舊策略、配置與 campaign 不遷入。刪除前保留既有未提交的 V2 改動與證據，之後驗證 V2 不再 import 已刪除的路徑。

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
  execution_models.py
  order_manager.py
  orchestrator.py
  telemetry.py

config/market_maker_v2/
  lighter_btc_volume.example.yaml

scripts/
  analyze_mm_v2_session.py

docs/mm_v2/
  OBJECTIVE.md
  ARCHITECTURE.md
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

- 保留舊 `MarketMakerConfig` 的相容層；
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

# 6. VolumeQuotePolicy 起始版規格

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

這是首版**相對 reservation price 的報價 baseline**，不是每筆成交 cover fee 的證明，也不是所有可行做市策略的數學下界。買賣可能在不同 reference／inventory 下成交，還有 adverse selection、funding 及退出成本；必須以整段真實現金流判定。減倉／止損不得重新加入 entry breakeven 限制。首輪保留公式以隔離變因，先量測 distance／實際 fills／成交後 markout；若要測更積極的 inventory-reducing price，另以既有風險額度內的單一實驗比較，不能偷偷降低 baseline 或放寬 stop。

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
maker_turnover_per_wall_hour
fills_per_quote_hour
```

新增 wall-hour 等比較指標為 §0.2 後的待實作報表契約；現有 `SessionReport` 尚不提供完整 candidate aggregation，不能把上述清單視為全部已完成。

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

## 10.1 使用 V2 自有執行模組

不要立刻重寫現有 mutation lifecycle。

新增窄介面：

```python
class ExecutionPort(Protocol):
    async def reconcile_quotes(self, plan: QuotePlan) -> ExecutionResult: ...
    async def cancel_all_managed(self) -> ExecutionResult: ...
    async def flatten_ioc(self, intent: FlattenIntent) -> ExecutionResult: ...
    def snapshot(self) -> ExecutionSnapshot: ...
```

`VolumeExecutionPort`／`BoundedExecutionPort` 使用 V2 自有 `order_manager.py` 中的 `MarketMakerOrderManager`，資料契約位於 `execution_models.py`。`config.py` 的 `ExecutionSettings` 只包含必要 execution 欄位，不依賴已移除的 V1 config。

## 10.2 保留安全語義，不保留舊策略依賴

V2 strategy 不得直接：

- 存取 slot private fields；
- 解析 current reason string；
- 新增 controller-specific branch；
- 新增 campaign telemetry；
- 將策略參數或 episode 經濟判斷塞入 execution manager。

Controller、toxicity、intent-attribution 舊政策支援不遷入。策略只使用窄 public ports；必要安全測試由 V2 suite 承接，不保留 V1 測試／package 作為執行依賴。

## 10.3 不另建第二套 order engine

§0.1 授權的是為移除 V1 而進行的必要安全模組遷移，不是重寫交易所 mutation lifecycle。保留已驗證的 exact terminal semantics，不同時引入另一套 order engine 或擴充策略。

---

# 11. Testing Strategy 重整

## 11.1 不搬移 649 個 V1 tests

依 §0.1 刪除 V1 專用 policy suite。V2 自有 execution-safety 測試承接實際仍使用的 cancel／uncertain／IOC／ownership 契約，不複製舊 controller／campaign 測試。

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
lighter_grid_regression
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

## Phase 0：Freeze 與重整契約（歷史階段）

本階段的原凍結動作已成為歷史；目前工作樹保留／移除範圍以 §0.1 為準。

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

目前輸入契約（V2-only）：

- external-BBO JSONL：每列包含 `timestamp`、`symbol`、`external_bid`、`external_ask`、`tick_size`；金融值使用字串，時間為含時區 ISO 或 epoch seconds。
- 另以 `--fee-evidence` 提供 JSON：`authenticated: true`、`observed_at_utc`、`maker_fee_rate`、`taker_fee_rate`。這是明確的歷史費率證據，不代表工具重新 authenticated，也不能代替 live startup 的 current fee 查證。
- 不再解析 V1 shadow／Gate 報告；既有 feasibility 結果只保留為歷史證據。

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

2026-09-05 review 後，實際後續工作先依 §19 R1–R3 執行：修 cleanup／雙邊與恢復報價、帳務／行情時間契約、完整 rolling API reserve，再重跑 timed dry。以下既有 replay 與 dry 的通過紀錄不撤銷，但不足以覆蓋新發現的真 execution 路徑。

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

開始前完成 §19 R4 的小型 analyzer 與資料覆蓋檢查。候選使用相同預先固定窗口，在多個時段交錯排序，避免將行情先後誤判成參數效果；後續以另一段未參與選參的窗口確認。不得把一筆 fill 當成獨立統計樣本，也不因三次結果恰好為正就宣稱穩定 fee cover。

### 產出 Pareto

每個 candidate：

```text
maker turnover/wall-clock hour
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

聚合先加總 `maker turnover`、`net`、`gross`、`fees` 與時間再相除：`cost_bps = -Σnet / Σmaker_turnover × 10000`，不可平均每場 bps／ratio。所有預先登記窗口與失敗均保留；若某場 final accounting 不完整，整組不能宣稱 economic GO。另列不含 funding 的交易 net，避免靠正 funding 或單邊 drift 誤認 spread capture；最終 all-in 仍包含 actual funding 及全部退出成本。零費用時 fee-cover ratio 不適用，以 net/cost 判定。

不是：

```text
單次 PnL最高
```

### Kill criterion

若三個 spread candidate 都未在完整帳務與足夠觀測下同時達到：

```text
all-in net cost <= 0
fees > 0 時 fee cover >= 1；零費用以 net/cost 判定
且固定時間窗的 maker turnover 達到預先約定的量級
```

則標記：

```text
no_feasible_point_in_tested_region
```

若成交太少、時間覆蓋不足或帳務不完整，改記 `insufficient_economic_evidence`。使用者的絕對 volume 目標尚待測量後設定，不以退役 V1 的低成交量作成功門檻。無可行點時停止盲目加參數，先判斷是 execution uptime、queue/fill、adverse selection 或 forced exit 成本；再决定是否值得在相同風險上限內做一個 inventory／quote persistence 實驗。費率、symbol 或 hedge 是另行評估；points 不以未兌現估值冒充交易 fee cover。三個 candidates 不足以作全市場不可行的結論。

---

## Phase 9：Inventory Parameter Matrix

只有 Phase 8 出現可行／接近可行且成本來源可解釋的 spread 點後才測；或依 §19 R4 證明最低下單量與 inventory bands 使原候選無法表現 continuous quoting 時，先做一個保持原風險額度的可執行性修正。測：

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

## Phase 12：V1 Decommission（使用者已提前授權）

依 2026-09-05 使用者明確指示，本次即移除 V1，不再等待 V2 4h／24h GO：

1. 工作樹只保留 V2 Market Maker runner／runtime／config／scripts／tests／docs。
2. 必要 execution safety 移入 V2 自有模組與測試，沒有舊 package import。
3. 刪除 V1 文件，不建立 `docs/archive/mm_v1/`；Git 歷史保留。
4. 更新 README、AGENTS 與現行 V2 文件，移除舊操作入口及 legacy-suite 要求。
5. V2 的 dry／live／economic gate 仍獨立驗收；README 不得把「唯一版本」當成 production 推薦。

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
maker_turnover_per_wall_hour
```

輔助：

```text
maker_fills_per_quote_hour
maker_turnover_per_quote_hour
quote_uptime
```

依 §2.2 計完整固定時間窗，另報雙邊 uptime、實際 elapsed-hour rate 與 turnover / allocated capital；不以 completed episode 或有掛單秒數作主分母。約 299 USDG 只作測試資金背景；尚無絕對成交量門檻，不虛構 GO。

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
V2 execution／Lighter／Grid regression
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

1. V1 已依 §0.1 從工作樹移除；V2 execution 不再依賴舊 package，且必要安全契約有測試覆蓋。
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
9. 以完整 all-in accounting 的聚合及獨立確認窗口，找到 `net cost <= 0`、費用非零時 `fee cover >= 1`，且固定時間 turnover 達預定目標的 candidate。若只找到正 acquisition cost，須明確列為「未達 fee-neutral」，只有使用者另接受正成本目標才可依新目標驗收。
10. 若找不到，回報已測條件下無可行點或證據不足及限制原因，不聲稱三個 candidates 證明全市場不可行，也不繼續盲目增加 guard。
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

---

# 19. 2026-09-05 Review：從目前 Phase 7 接續

## 19.1 審查结論與證據範圍

`260be69` 的策略分層值得保留：quote policy 未使用 entry breakeven 錨；ledger 接受虧損交易、區分 maker/taker 費用並計入退出成本。主要缺口在 execution/data 接線、正常事件的恢復能力、dry fidelity 與經濟驗收，沒有證據支持再全面 rebuild 或加入多層／toxicity／ML。

本輪從乾淨 worktree 重新執行 V2 **345 tests PASS**；full repo **619 tests，8 failures + 4 errors**，符合現有 Grid/Lighter baseline。除此之外，以真 V2 ports／session／ledger 和既有 fake exchange 做以下離線重現。通過既有 suite 不代表下列問題已被覆盖；本輪未修 runtime，也未重跑網路 dry／live。下表行號指 review 基準，修改後應以 symbol 和契約定位。

| ID / 優先度 | 位置 | 可重現觸發、影響與最小方向 |
|---|---|---|
| F1 / P1 | `execution_port.py:485`；`cancel_all_managed:281`；`orchestrator.py:124` | 已有掛單時，純讀取 `TimeoutError` 把 bridge `_failed` 永久設為 true；cancel／bounded exit 又要求 HEALTHY。完整 session 重現 **creates=1、cancels=0、open orders=1，然後 disconnect**。區分 data refusal 和未知 mutation；前者須保留對已知 ID 的安全撤單與對帳能力，不能靠清掉所有 uncertainty 來恢復。 |
| F2 / P1 | `orchestrator.py:406–414,470` | passive reducing POST_ONLY 建立後，下一次 snapshot 一次讀取失敗便跳過 `bounded_exit`；`_cleanup_attempted` 阻止 finally 再收尾。重現 **position=0.1、open orders=1、cancels=0、execution HEALTHY，然後 disconnect**（synthetic quantity）。Grace 中止仍須在原 deadline／同一退出額度內執行安全收尾，不能重開 3 次 IOC 額度。 |
| F3 / P1 | `execution_port.py:499–525`；`order_manager.py:356,555` | POST_ONLY canceled 啟動 book-refresh generation fence，但 V2 沒有 production caller acknowledge；唯一 caller 在 OM test。重現第一次 canceled 後每隔 10s 授權，共四輪仍 **CONFIRMED／0 新單**。Bridge 需證明 rejection 後的新可信 book，再 acknowledge 對應 generation，保留 cooldown。 |
| F4 / P2 | `execution_port.py:506`；`order_manager.py:613` | 每 3s BBO 移動超過 reprice threshold，bridge 全撤，OM 每輪一筆 create 即 return，flat 時 BUY 優先；重現 **六輪只有 BUY、沒有 SELL**。Dry 直接建立雙邊，掩蓋差異。修正有界 cycle 內的逐筆重新授權／補另一邊流程，不能直接取消 one-create safety boundary。 |
| F5 / P1 | `lighter_runtime.py:467–484`；`market_state.py:85` | normal revision 已確認 own order 撤銷，而下一份 WS book 尚未抵達；own 變更造成 cache miss，同一 source time 再 update 被拒。真 session 重現 **20s 目標在約3.095 fake秒退出，book 僅38ms舊**。Ownership 變更需與 book 對齊；有界等候可證明覆蓋變更的新 book，不能 restamp 或盲目從舊 book 扣新 own size。 |
| F6 / P2 | `inventory_governor.py:211–222`，對照 `:148–150` | Stop 以 high-water drawdown 判定，但新單 reserve 只計負的 realized net 等損失。真 ledger synthetic 路徑先 +100 再 -99，net=+1／DD=99；cap=100／stop=10 仍授權新單，下一個合法 stop 後 DD=109。需以 **current drawdown** 的剩餘額度預留風險，同時保留起始 equity loss 限制；不能把歷史最大 DD 永久扣掉，造成恢復後失去容量。 |
| F7 / P2，public contract | `session_ledger.py:357–359` | Final account `observed=100`、`inputs_observed=0` 時 `fresh(100)=False`，但 `finalize` 仍回 complete。應納入 `final.fresh(now)`，過期即 incomplete。現行 Lighter provider 有額外 freshness 檢查，**尚未證明正常 live 路徑可觸發**，優先度低於 F1–F6。 |

既有 No-Go 仍獨立成立：source age `-17.3067ms` 超過 host quantum；高改價 fixture 的 rolling REST `26,700`、現 WS 握手換算約 `308/min`；Unified nonflat／funding liveness 未驗證；正常 fill 穿過多來源 account bracket 會觸發 refusal。詳細歷史以唯一 [EXPERIMENT_LOG](mm_v2/EXPERIMENT_LOG.md) 為準，不將這些已知問題當成新發現。

## 19.2 修正順序與可驗收成果

每個 R 是獨立、可 review 的工作單位，不另建 campaign／checkpoint 文件；以下為驗收要求，實際完成範圍見 §19.4，未授權 live 或 Git mutation。

### R1 — 修復退出路徑（先於任何下一次 run）

- 修 F1／F2。禁止新增風險與可安全 cleanup 分開；只有已知 ID、確定 ownership 與可核對 mutation 的撤單才可執行。未知終態須先 reconciliation，不把 HALTED 改成無條件可送單。
- Passive grace 是退出的可選前段；資料錯誤中止 grace 後，繼續同一次 bounded cancel／fresh truth／IOC。共用原 deadline、固定價格及最多三次額度，失聯或無流動性仍如實報 residual。
- 驗收：上述兩個 session 重現改為 recovered truth 下 final authenticated `0/0`；unknown submit/cancel、持續資料故障、deadline、partial IOC 仍 fail closed，不能誤報完成。實際已知訂單不能只因純讀取錯誤被跳過撤單。

### R2 — 修復持續雙邊與風險額度

- 修 F3／F4／F6，補 F7。使用現有 execution public ports；不要直接操縱 slots 或另建 engine。
- POST_ONLY rejection 必須可在新 book／cooldown 後恢復，舊 book 仍不能解 fence。兩邊有效時，改價、partial fill、fee change 不能讓固定一邊永久餓死；每筆 create 後重新授權，不以一次舊 account proof 連下兩單。
- 讓 dry／replay 覆蓋與 live 相同的逐筆 execution cadence；報告 actual working 的買側、賣側和雙邊時間，不能只看至少一張單。
- 驗收：真 OM＋fake exchange 的多輪移動 BBO、rejection 後恢復、profit→drawdown→下一單 reserve、stale final proof；測 public behavior，保留 bounded exit／cancel race 回歸。

### R3 — 整合資料一致性、clock 與 API 退出餘額，再驗 Phase 7

- 修 F5。Book source/receipt time、own-order version、account/fill watermark 分別保留；fixture 不得每次讀取都改寫 source time 或瞬間把 own order 混入 book。
- Clock 要處理跨主機誤差與 clock jump，先定義可量測的誤差界線；高解析 monotonic 只解決本機計時粒度。對 stale／future／jump 分類，不能無限加等待或删除 source-age 檢查。
- 對正常 fills／REST-WS 到達不同步，暫停新增風險，在**同一 deadline 與 read budget**內取得 coherent proof；未知、超時或耗盡才進失敗收尾。保留 exact cash bridge，不以 epsilon 填平帳務差額。Fill source time 與 ingestion time 的差異也須在 hold-age replay 中驗證，不能用較晚的 audit 時間證明實際持倉未超時。
- 先在現有窄接線去除同一 cycle 的重複讀取，再評估持續 WS account/order 狀態加事件後 reconciliation 是否有足夠完整性證據。官方 channel 有 nonce 並不自動代表任意增量可當完整 authenticated order list；需要先驗協定。
- 明確預留退出需求：任意 rolling60 window 的已用讀取量＋正常 cycle 新增量＋最壞安全收尾 reserve 必須在 applicable REST／WS 上限內。計入 auth/history、unsubscribe/subscribe、keepalive、fills、cancel terminal、三次 IOC 及 final proof；共用 IP／L1 的其他工作負载也占額度。額度不足先停止新增風險，不能等 429 才退出。
- 驗收順序：真 execution 路徑離線測量（calm、每輪改價、多 partial fills、正常 arrival race、stop／三次 IOC）→ 短唯讀協定檢查 → 完整30min T3。保留 smoke_03 歷史；資料接線大改時只重做受影響 smoke，不盲目重跑原失敗程序。Dry 全程零 mutation，並保留獨立 postflight 與 process-exit 證據。
- 盡量限制在 V2；若 shared Lighter opt-in stream 必須修改，說明 Grid 影響、保留原預設行為並跑相關 Lighter／Grid 回歸。

### R4 — 用小型經濟報表與可執行配置，準備 Phase 8

- `scripts/analyze_mm_v2_session.py` 尚未存在。完成最小 JSONL→單場／candidate aggregate 表格即可；復用 ledger 定義，拒絕 incomplete economics，不另建 recorder database、campaign authority 或新的設定框架。
- 先補密集、可揭露覆蓋率的外部 book／trade 觀測。既有 Phase 1 僅184列、無1s配對、5s配對12/183，足以示範工具，不能證明 fill opportunities。記 effective quote distance、quote lifetime、revisions 與成交後1s/5s markout（診斷，不作新 blocking controller）；沒有可信成交時只能報 touch／trade-through 候選機會，不能當自身 queue fills。
- 對每個 candidate 產生 flat／soft／hard 的**實際可執行數量表**，通過 tick、lot、minimum notional、已掛單最壞 exposure 和退出 reserve。例：純示意 BTC=80,000、minimum notional=10 時，example 的 `order_size=soft_limit=0.00020`；一整筆 BUY 後增加風險側縮成0.00010、notional約8，被 runner 刪去，只剩 SELL。即使全撤重算也相同，因此 example 不保證 continuous accumulation。這是配置耦合，不是要恢復 V1 或無條件放大 size。
- 使用者約299 USDG 背景下，同報 `turnover / allocated capital`、maximum gross exposure 及每10,000 USDG成交的净成本；帳戶餘額、配置資金與風險額度分開。成交目標待資料支持後約定；不直接用全額資金當 inventory 或 loss cap。
- 策略優先保持簡單單層；若有交易但經濟差，先用 fills 的 realized spread／markout／forced-exit 分解判定原因，再測一個 quote persistence 或 inventory 變因。較複雜 reservation 模型只作參考，不能代替本市場的 fill-versus-distance 證據。

### R5 — 執行授權 canary，依證據決定是否延長

- R1–R4 的對應驗收完成後才提出具體 reviewable run：network／account／symbol、order size、預定窗口、hard exposure、單場與整組累積 loss、flatten price/attempt/time limits、停止條件。仍需當場明確 live＋bounded-flatten 授權。
- 先做最小 execution/accounting canary，驗證真實 nonflat、partial fills、fee／funding 與正常撤換單可用性；通過不算 economic GO。再按 Phase 8 三組 spread 的固定窗口交錯比較，所有成本与失敗保留；選出可行或接近可行點後，依 Phase 9 驗證一個 inventory 變因及獨立確認窗口。
- 只有穩定 execution、完整 aggregate fee cover 及量級目標有證據才延長2h／4h；24h persistence／recovery 仍依 Phase 11，不先為短期未知 economics 建大型基礎設施。

## 19.3 外部資料與推論界線

2026-09-05 查閱 [RH 官方 rate limits](https://apidocs.rh.lighter.xyz/docs/rate-limits.md)：Premium REST 為24,000 weighted requests／rolling minute，WS client messages為200／minute、按IP計，超限可能影響連線。這支持 R3 必須保留完整退出讀取餘額；不是本輪已驗證使用者帳戶 tier 或可用額度。

[RH 官方 WebSocket](https://apidocs.rh.lighter.xyz/docs/websocket.md) 說明 book 初始全量與 nonce continuity、account_all 重訂閱全量及各 account channel。R3 的協定重構方向是待驗證設計，不能從文件直接推論現有多來源帳務已原子一致。

[Avellaneda–Stoikov 原論文](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf) 同時處理 inventory reservation 與隨報價距離變化的成交到達率；本文据此把 fill/quote distance 觀測列入 R4，沒有假設該論文的市場模型已適用於 RH BTC。[Hummingbot V2 架構](https://hummingbot.org/strategies/v2-strategies/) 的資料、策略與執行分工支持保留目前 V2 邊界，不構成搬入整個框架的理由。

## 19.4 修復進度（2026-09-05，worktree on `260be69`）

- **R1 已修並通過離線驗收**：quote 純讀取 timeout／task cancellation 不再永久鎖住已知委託 cleanup；中斷 mutation 仍由 OM 保留 uncertainty。Passive grace 讀取失敗後進入同一次 bounded exit，沿用原 deadline／最多三次 IOC 額度。兩個原 session 缺口在恢復可信資料後均驗證 authenticated `position=0/orders=0`，持續失敗與 unknown execution 仍不得假稱成功。
- **R2 程式缺口已修並通過離線驗收**：一個原 10s execution deadline 內最多兩次 one-create reconcile，中間重新核對 account／risk／market；首筆即成交也先重算持倉再掛減倉側。第二次讀取 timeout 不加時，保留第一筆已送出計數。POST_ONLY fence 必須由 rejection 後新可信 book 解鎖，cooldown 保留。Reserve 同時計入起始 equity loss 與 current drawdown headroom，恢復後不永久扣歷史 maximum DD；finalize 檢查底層 account inputs freshness。實際觀測新增 buy／sell／two-sided quote seconds，時間聯集不重複相加，缺 side 證據為 unavailable。網路 dry 仍是零成交意圖模型；execution cadence 用真 V2 OM＋fake exchange replay 驗證，不將 dry 通過當作 live 接線驗收。
- **R3 部分修復，仍未達 T3／canary gate**：F5 改為 authenticated target-market order nonce → 原始 receipt 的 book → closing order nonce；opening/closing 完整訂單相同且 book nonce 落在兩端之間才供 quote 使用。OM 與 account 在同一 cycle、同一 mutation generation、3s 內交接一次完整 order observation；失敗／取消／新 cycle／mutation 立即作廢。只保留 cash 後的一次 full account_all，仍核對全帳戶 identity、持倉、order counts、funding、exact cash bridge 與 trade-history counter。Terminal fills 改用一次100-row history 核對多筆 exact IDs，缺漏／重複／成交量不符仍拒絕。延遲且不重打時間戳的 book fixture 通過；短真實唯讀 nonce／unsubscribe 協定與三次 aligned account/book 檢查通過，但僅證明 flat 現況，沒有真實 fill／cancel 因果證據。
- **Clock 已有主機證據與可攜式拒絕條件**：Windows Time 原未啟動，獨立 NTP 顯示慢約0.4s，對應 source age 約−359ms。依使用者明確授權啟用 Automatic／Running 並校時後，NTP 偏差降為約5–8ms，唯讀對齊通過。Runtime 保留 strict source age 0..3000ms／receipt 3s，以及最多一個 host quantum、cap20ms 的等待；新增相鄰 wall elapsed 與 monotonic elapsed 差額超過50ms的 jump refusal，這是連續性界線，不是允許 future source。VPS 入口 `Desktop/vps_lighter/Open-Grid-Tradexyz-VPS.cmd` 指向 SSH alias `grid-tradexyz-vps`；唯讀核對 chrony active、NTP synchronized、偏差約2µs。未改 VPS 設定／部署；其 checkout 仍是舊 MM 分支 `feat/lighter-market-maker-mvp`／`2de97c4`，不能當成 V2 部署或測試通過。
- **API reserve 仍是獨立 No-Go**：同一90 fake-second高改價 fixture，合併 order observations／account_all 後，原 public-method 模型 WS 下限由330降至195/min；該模型漏算 shared adapter 的 create lookup／cancel terminal reconciliation／get_order 內部請求。按已讀 source 補入每 create 至少300、每 cancel 至少400、terminal get_order 400 的 REST 權重，峰值下限為35,600；terminal history 合批後降至30,000，WS195不變。仍未含 retry／全部 confirmation latency／auth／keepalive／三次IOC／final proof，不能以195低於200宣稱餘額成立。短期不得以加長 cycle、假回 terminal 或放寬 freshness 掩蓋；下一個工作單位須處理 shared confirmation reads 的重複證據與完整 request admission／退出 reserve，並補 normal arrival-race recovery／fill source-time hold-age。
- 受影響的10min dry smoke已完成602.247s，flat authenticated0/0、rolling60量測REST14,100／WS113（不含native signer startup checks）；只驗證新的flat read path與校時後穩定性，載入版本及後續補碼驗證範圍見唯一EXPERIMENT_LOG；不取代真 execution／三次IOC 的 API reserve 驗收，30min T3 尚未開始。R4 analyzer／candidate 配置表與 R5 授權 canary尚未開始；18個設定欄位、size/risk與Grid production不變。Shared改動僅 opt-in Lighter read stream及其target-market snapshot路由，原Grid預設REST不變；有真實唯讀account連線，沒有交易／帳戶mutation、commit或push。
