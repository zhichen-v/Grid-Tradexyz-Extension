# Market Maker MVP 操作指南

本指南適用於單一 Lighter 永續合約交易對的 Market Maker MVP。所有命令均從 repository 根目錄執行。

> **硬性安全規則**
>
> - 30 分鐘 dry-run 與 Phase 8 live rollout **只能由操作者明確執行**；automated test 不得執行這些步驟，也不得連線送出真實交易 mutation。
> - 一般報價必須為 `POST_ONLY`；嚴禁 self-trade、跨自有帳戶互相成交或任何形式的洗量。
> - 程式停止時只撤單，**不會自動 flatten position**。停止後的持倉由操作者另行評估與處理。
> - 每次停止（含緊急停止）最後都必須透過 authenticated REST 或 Lighter 交易所介面確認目標 symbol 的 `open orders = 0`。未歸零、無法查詢或取消結果不確定，都屬事故，不能視為正常停止，也不得重新啟動。

## 1. 環境安裝

需求：Python 3.12、`uv`、repository 既有依賴。PowerShell：

```powershell
Set-Location 'C:\Users\kyle.chen\Desktop\vps\Grid-Tradexyz-Extension'
uv venv --python 3.12
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
```

若 `.venv` 已存在，勿任意重建；只需安裝或同步 `requirements.txt`。先執行離線/Mock 測試：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Market Maker integration test 必須使用 fake adapter，不能存取 live network 或送單。

## 2. Credentials 與帳戶隔離

Market Maker YAML 不保存 credentials。程式沿用既有 credential bootstrap：

- 未指定 wallet profile：讀取既有 `config/exchanges/lighter_config.yaml`。
- 指定 wallet profile：只讀取 `.env.wallets/<profile>.env`，不再讀取或混入預設 `lighter_config.yaml`；缺少任一必要欄位即中止。CLI 使用 `--wallet-name <profile>` 或既有 `--<profile>` shortcut。
- Profile 使用的欄位名稱為 `LIGHTER_API_KEY_PRIVATE_KEY`、`LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、`LIGHTER_NETWORK`；可選 `LIGHTER_EXPECTED_L1_ADDRESS`。
- `LIGHTER_NETWORK` 必須與本次 rollout 的 Lighter 環境一致；testnet 與 live 不得混用。

`LIGHTER_EXPECTED_L1_ADDRESS` 只做格式驗證；Market Maker 不會透過 Adapter 私有欄位宣稱 ownership 已驗證。若有設定此欄位，啟動前仍須另外執行既有 authenticated read-only preflight，確認 account index 的 L1 owner 正確。

不得將 private key、auth token、wallet profile 內容或 signer object 寫入 Market Maker YAML、Git、測試 fixture、命令列或 log。操作時只選擇既有 profile，不複製或顯示其內容。

強烈建議使用專用 sub-account；至少也必須讓目標 symbol 由本程序獨占。啟動前確認同一帳戶與 symbol 沒有 Grid、Volume Maker、另一個 Market Maker、手動掛單或其他自有帳戶策略可能互相成交。不可依賴交易所 Self-Trade Prevention 代替此隔離。

## 3. Config 檢查

範例：`config/market_maker/lighter_btc_mvp.example.yaml`。複製成操作者專用且不含秘密的設定檔後再修改。金融數值維持 YAML 字串，避免浮點誤差。

| 欄位 | 操作意義 |
|---|---|
| `exchange`、`symbol` | MVP 僅支援 `lighter` 與單一 symbol。 |
| `order_size` | 每側基礎數量；必須符合 size step、base/quote minimum，且不得大於 `max_position`。 |
| `quote_mode` | `both` 為雙邊；Step B 僅可使用 `bid_only` 或 `ask_only`。模式切換前須先正常停止並確認 open orders 為 0。 |
| `base_half_spread_ticks` | 單側距離，不是完整 bid-ask spread；至少 1 tick。 |
| `max_raw_spread_bps` | External BBO 超過此門檻時 midpoint 視為不可信，進入 `PAUSED_MARKET` 並先撤單；fresh book 回到門檻內才恢復。 |
| `max_inventory_skew_ticks` | 庫存偏移上限；long 時報價中心下移，short 時上移。 |
| `reprice_threshold_ticks` | 與現有單差距達此門檻才換價，降低 churn。 |
| `maker_fee_rate` | 依實際帳戶 tier 核對的單邊 maker fee；不可假設永久為零。 |
| `min_profit_buffer_bps` | 費率之外的最低 spread buffer；不代表獲利保證。 |
| `max_position` | 正負方向的絕對持倉上限。 |
| `soft_position_ratio` | 進入 soft zone 後縮小增加風險的一側。 |
| `hard_position_ratio` | 進入 hard zone 後只允許 reduce-only 減倉側。需滿足 `0 < soft < hard <= 1`。 |
| `refresh_interval_ms` | quote loop 的最低刷新週期。 |
| `min_order_lifetime_ms` | 一般換價前的最短存活時間。 |
| `stale_book_seconds` | 訂單簿超時門檻；超時即停止 create 並進入 fail-closed。 |
| `stale_position_seconds` | 持倉超時門檻；不得把 unknown position 當成 0。 |
| `position_poll_interval_seconds` | REST position safety sync 間隔。 |
| `order_sync_interval_seconds` | REST open-order reconciliation 間隔。 |
| `health_check_interval_seconds` | Exchange/WS health check 間隔。 |
| `max_consecutive_errors` | 連續錯誤達門檻後暫停。 |
| `error_cooldown_seconds` | 錯誤後冷卻時間；不可 busy retry。 |
| `max_mutations_per_minute` | create/cancel mutation budget；必要安全撤單優先。 |
| `post_only` | 必須為 `true`。 |
| `exclusive_symbol_control` | `true` 表示本程序獨占該 symbol，亦是允許 startup `cancel_all` 的前提。 |
| `startup_open_order_policy` | `abort` 遇既有單即停止；`cancel_all` 僅在 symbol 確實獨占時允許，且撤後必須確認為空。 |
| `unknown_order_policy` | MVP 安全預設為 `pause`；未知單不可猜測為本程序所有。 |
| `cancel_on_shutdown` | 應維持 `true`；停止時撤除 managed orders。 |
| `dry_run` | 安全預設為 `true`；live 必須由操作者明確改成 `false`。 |
| `log_status_interval_seconds` | 結構化狀態輸出間隔。 |

Live 前另需核對 exchange metadata 的 price tick、quantity step、minimum amount，以及 `order_size * target_price` 是否達 quote minimum。不一致、精度不合法或 minimum 不足時應中止，不能自動放大真實訂單。

## 4. Startup open-order policy

啟動前先透過 Lighter 介面查詢該帳戶與 symbol 的 open orders：

- `abort`（建議）：任何既有 open order 都中止啟動；先由操作者辨識與處理。
- `cancel_all`：只可搭配 `exclusive_symbol_control: true` 與確實獨占的 symbol。取消後必須再查 REST/交易所介面，確認 `open orders = 0` 才能繼續。
- Dry-run 只報告既有訂單，絕不因 startup policy 取消任何訂單。

執行中出現 unknown order 時，停止新增訂單並 pause。不要接管、猜測 ownership 或換 client id 重送；先以 REST/order history reconciliation。

## 5. 30 分鐘 Dry-run Gate

此 gate 由操作者手動開始、監看及停止；automated test 不執行。Dry-run 會連線讀取 metadata、orderbook、position、open orders 及訂閱 WS，但不得呼叫 `create_order`、`cancel_order`、`cancel_all_orders` 或其他帳戶 mutation。

建議命令：

```powershell
.\.venv\Scripts\python.exe run_market_maker.py config/market_maker/lighter_btc_mvp.example.yaml --dry-run --debug
```

有 `uv` 時的等價 pipeline 命令：

```powershell
uv run python run_market_maker.py config/market_maker/lighter_btc_mvp.example.yaml --dry-run --debug
```

若使用既有 named profile，在命令末尾加 `--wallet-name <profile>` 或既有 `--<profile>` shortcut；不要在命令列放 credential 值。

連續執行至少 30 分鐘並留下下列紀錄：

| 項目 | 通過條件 |
|---|---|
| CPU | 無 busy loop，使用率適合目標主機。 |
| RSS memory | 無持續成長；記錄開始值、峰值與結束值。可用 Task Manager 或依程序 PID 執行 `Get-Process` 觀察。 |
| WS reconnect | 記錄次數與原因；無 reconnect storm 或 callback task explosion。 |
| REST request rate | 符合設定週期，無 429 storm。 |
| Target quote | 持續記錄 `mid`、`reservation_price`、`target_bid`、`target_ask`、inventory ratio/skew。 |
| No-cross invariant | 每個有效 cycle 都滿足 bid/ask 不交叉，且符合 post-only 邊界。 |
| No-mutation invariant | 真實 create/cancel/cancel-all 次數均為 0；只可出現 `would_place`、`would_cancel` action plan。 |
| Error count | 記錄錯誤與 pause reason；無未解決 critical error、invalid tick/size 或 repeated 429。 |
| Data/risk | Book、position 保持可信；worst long/short 未超限，position/skew 方向正確。 |

任一項失敗即為 **NO-GO**：停止、保留 log、修正並重新完整跑滿 30 分鐘。未通過不得進入 live rollout。

## 6. Live start

Live 僅能由操作者在完成 dry-run gate 後執行。優先使用 testnet；若只能 live，使用專用 sub-account 與交易所允許的最小安全資金/數量。

啟動前逐項確認：

1. 帳戶、network、symbol 與 wallet profile 正確。
2. 該 symbol 無其他 bot 或手動策略；不可能發生 self-trade。
3. Open orders 符合 startup policy。
4. Fee tier、funding、margin、tick、step 與 minimum 已人工核對。
5. `post_only: true`、`cancel_on_shutdown: true`、風控上限與 mutation rate 保守。
6. Live 設定中的 `dry_run` 已由操作者明確改為 `false`，命令中不含 `--dry-run`。

```powershell
.\.venv\Scripts\python.exe run_market_maker.py <live-config-path> --debug --wallet-name <profile>
```

或使用既有 `--<profile>` shortcut。看到 `ACTIVE` 前必須先完成 metadata、book、position、open-order sync；任何 startup error 都不應送單。

## 7. 正常停止與驗證

1. 在執行 Market Maker 的 terminal 按一次 `Ctrl+C`。
2. 等待 runtime 進入 `STOPPING`，停止新 quote cycle，完成 managed-order cancel、REST reconciliation 與 disconnect。
3. 檢查 exit code 與 critical log；正常 `Ctrl+C` 應為 exit code `0`，cancel failure、uncertain cancellation 或其他非零 exit 都不是正常停止。
4. **另外透過 authenticated REST 或 Lighter 交易所介面查詢同一帳戶、同一 symbol，確認 `open orders = 0`。只看本地 slot、console 的 `STOPPED` 或程序消失都不算確認。**
5. 檢查實際 position。程式不會自動 flatten；是否及如何處理持倉由操作者在交易所介面另行決定。

若 open orders 非零、查詢失敗或狀態仍 uncertain，立即依「緊急人工程序」處理並記錄為事故，禁止宣告正常停止或重啟。

## 8. Logs 與監控

- Market Maker runtime state、quote、risk、pause reason，以及 `would_place` / `would_cancel`、create/cancel attempts/success counters，輸出至 console 與 `logs/market_maker.log`。
- Exchange adapter 的既有檔案 log 至少包含 `logs/ExchangeAdapter.log`。
- `logs/market_maker.log` 採既有 line-limited logger；長時間 gate 仍應由操作者另行安全保存所需區段，避免輪替後遺失證據。
- 應監看 runtime state、book/position age、target/live quotes、worst exposure、reconnect、reconciliation、unknown/uncertain order、HTTP 429、mutation limiter 與 error count。
- Runtime 會對目前 signer key 套用全域 log/traceback 遮罩；Log 仍禁止主動包含 private key、完整 token、`.env` 內容、credential config 內容或 signer object repr。

## 9. 常見錯誤與處理

| 現象 | 操作 |
|---|---|
| HTTP 429 | 暫停 create，讓 shared cooldown 生效並降低 mutation；不可快速重啟或無限 retry。若安全撤單也不確定，進入 pause 並依緊急程序確認訂單。 |
| Stale book / `PAUSED_DATA` | 不沿用舊價；確認 WS、最近訊息時間與主機網路。資料恢復後必須先重新 sync，不可直接跳回 ACTIVE。反覆發生則停止。 |
| Stale/unknown position | 不得假設 position=0；停止 create、撤除可確認報價，核對 REST position 後重新 sync。 |
| WS unhealthy | 停止新單，REST sync 並撤除可確認報價；不可一邊無限重連一邊持續報價。 |
| Submission uncertain | 視為訂單可能已存在，禁止 duplicate 或更換 client id 重送；查 open orders/history reconciliation。 |
| Cancellation uncertain | 視為訂單仍 live 且計入 worst-case exposure，禁止 replacement；查 REST/history。無法確認即事故。 |
| Unknown open order | 預設 pause；由操作者辨識。非 exclusive symbol 不可 `cancel_all`。 |
| Post-only canceled | 讀取新 book，下一 cycle 重算；不可原價立即盲重送。 |
| Invalid precision/minimum | 中止啟動，重新核對 metadata/config；不得截斷或自動放大 live order。 |
| Insufficient margin | 停止高頻重試，檢查資金與風控；達錯誤門檻後維持 pause。 |
| Max position reached | 只能保留 reduce-only 減倉側；不可恢復固定雙邊 size。 |
| Shutdown cancel failure | Critical incident；不得將程序結束視為訂單已撤，立刻執行下節程序。 |

## 10. 緊急人工程序

適用於 stale/unknown order、cancel failure、程序卡死、風控異常、WS/REST 長時間不一致或任何疑似未受控訂單。

1. 先停止新的 mutation：發出 `Ctrl+C` 並禁止 watchdog/排程自動重啟。
2. 若程序沒有停止或仍可能重掛，終止該程序；記錄時間、帳戶、network、symbol、最後 runtime state 與 error，但不得記錄 secrets。
3. 登入正確的 Lighter 帳戶，使用 authenticated REST 或交易所介面查詢**該 symbol** 的 open orders；不要只相信本地 cache。
4. 人工取消該 symbol 的所有剩餘 Market Maker 訂單。只有在已確認 symbol/account 為獨占時，才可使用 symbol-level cancel-all；否則逐筆辨識處理。
5. 等待交易所回報 terminal 後再次查詢，必要時重複查詢；最終結果必須明確為 **`open orders = 0`**。
6. 核對 position 與最近 fills。程序不會、也不得自動 flatten；若需調整持倉，由操作者依帳戶風險另行決策。
7. 保存 console 與 `logs/ExchangeAdapter.log` 的相關區段，記錄 uncertain submission/cancellation、429、WS 狀態與人工操作。
8. 找到根因並重新通過 30 分鐘 dry-run gate 前，不得重啟 live。

若最後仍有任何 open order、REST/交易所介面無法確認為零，或 cancellation 狀態不確定，事件維持 **事故狀態**；不得標記為正常停止。

## 11. Phase 8：Testnet／最小資金 Rollout

以下每一步均由操作者逐步授權與執行，automated test 一律不得代跑。Testnet 優先；每步停止後都執行第 7 節的 `open orders = 0` 驗證。任一安全 invariant 失敗即退回 dry-run。

### Step A：Read-only

- `dry_run: true`，並以 `--dry-run` 啟動至少 30 分鐘。
- 驗證 metadata、book、position、open orders、WS/REST sync 與完整 dry-run gate。
- 確認 mutation 為零後才可進 Step B。

### Step B：單邊極小額

- 將 `quote_mode` 設為 `bid_only` 或 `ask_only`，完成同等單元／整合測試並重新通過 dry-run gate；不可手改 runtime 或在 live 帳戶刻意製造危險持倉來模擬。
- 驗證所有一般限價單為 `POST_ONLY`，並逐一確認 create、cancel 與 shutdown 的交易所回報。
- 未確認 cancel terminal 前不得 replacement。

### Step C：雙邊極小額

- 將 `quote_mode` 明確設回 `both`；切換前先正常停止並確認 open orders 為 0。
- `order_size` 使用交易所允許的最小安全量，`max_position` 僅為數個 order size。
- Spread 寬於最終預期，refresh 較慢、mutation rate 較低。
- 連續監看 30–60 分鐘：一側一張、no-cross、worst exposure、fills、cancel 與 pause 狀態。

### Step D：小額長時間

- 連續執行數小時。
- 觀察 position drift、cancel latency、ambiguous state、post-only cancel rate、quote uptime、maker fills、funding/fee 與 memory。
- 未穩定前不縮 spread、不提高 size；出現 unknown/uncertain order 或反覆 stale/429 即停止。

### Step E：逐步調參

每次只改一項並重做觀察與停止驗證：

1. Spread。
2. Size。
3. Inventory skew。
4. Soft/hard ratio。
5. Refresh interval。

不得同時大幅調整多個參數。每次變更都要記錄 config 差異、觀察期間、風險/錯誤指標與 GO/NO-GO 結論；穩定性未證明前維持上一個安全設定。

## 12. Phase 8 本地驗證紀錄與交接（2026-08-20）

> **目前狀態：STOPPED / Phase 8 Step B、Step C GO（附 shutdown observation）。** 2026-08-20 晚間已完成殘留倉位處置、direct-PTY Step B clean retry，以及 Step C 雙邊極小額 30 分鐘 live。獨立 authenticated postflight 為 Market Maker 程序 `0`、BTC/account open orders `0`、BTC position `0`；config 已恢復 `dry_run: true`。Step C shutdown 的兩筆 cancel acknowledgement 曾短暫缺少 terminal proof，但程式沒有盲目重送，reconciliation 與後續 history 均精確證明兩張單為 `CANCELED / filled=0`，目前沒有未解決 uncertainty。Step D/E 尚未開始；VPS 同步與測試仍不在本階段範圍。

### 12.1 本輪完成項目

- 完成 Market Maker 專項與完整離線測試；最後一次完整結果為 `371 tests / OK`。
- 新增 `quote_mode: both | bid_only | ask_only`，並以單邊模式完成 Step B dry-run。
- 新增 raw BBO spread guard；超過 `max_raw_spread_bps` 時進入 `PAUSED_MARKET`、撤除可確認報價且禁止 create。
- 強化 shutdown、stale data/position recovery、unknown/uncertain order、ambiguous submission、post-only cancel refresh/cooldown 與 mutation budget 的 fail-closed 行為。
- Lighter WebSocket 改用 SDK async lifecycle，避免同步 WebSocket executor worker 在 shutdown 後殘留。
- 修正 Lighter account WebSocket position 方向解析：SDK 的 `position` 是 magnitude，方向由 `sign` 提供；`sign=-1` 現在正確解析為 short。
- Periodic REST position 與已知 position 不一致、且沒有 fill 證據時，不再立即覆寫可信倉位；先進入 `PAUSED_POSITION`，再經完整 REST recovery 確認。
- Live reconcile 每個 cycle 最多只嘗試一筆 create，避免第一筆 create 等待期間到達的 terminal update 尚未處理就送出另一側。

### 12.2 修復前驗證與第一次 Step B live：NO-GO

修復前曾完成一次 `1811s` 的 `bid_only` dry-run：`360/360` cycles 成功、`would_place=17`，真實 create/cancel、fill、HTTP 429、WS reconnect 與 failed cycle 均為 `0`；後段 raw spread 約 `78.2 bps` 時正確進入 `PAUSED_MARKET`，停止前資源約 `134.9 MB / 31 threads`。但後續第一次 Step B live 揭露 WebSocket position `sign` 解析錯誤，因此該 dry-run **不得**作為修復版 rollout gate。

第一次 Step B live 的已知結果：

- 啟動前 authenticated preflight：account `5957`、BTC short `0.00020`、open orders `0`。
- 只建立一張最小量 `BUY 0.00020 / POST_ONLY / reduce-only` 訂單。
- 訂單約六秒後被本地 safety path 非預期取消；成交量為 `0`，實際 position 未改變，停止後 authenticated postflight 為 open orders `0`。
- 根因為 account WebSocket 將 `position=0.00020, sign=-1` 錯讀成 long，risk 因而暫時禁止 bid 並觸發 safety cancel；不是正常 reprice，也不是 shutdown cancel。
- 此輪判定 **NO-GO**。修正 parser、position-change recovery 與相關回歸測試後，才重新執行下節 dry-run。

### 12.3 修復後 30 分鐘 Step B dry-run：GO

使用 `config/market_maker/test_lighter_btc_mvp.yaml`，同時保留檔案中的 `dry_run: true` 並在 CLI 加上 `--dry-run`。觀察期間為 2026-08-20 02:09:02 至 02:39:27（Asia/Taipei）。

| 項目 | 結果 |
|---|---|
| 模式 | `bid_only`；`order_size=0.00020`；`max_position=0.00020` |
| 執行時間 | `1823.75s` |
| Cycles | `361 / 361` 成功，failed `0` |
| Position | 全程 `signed_position=-0.00020`，沒有方向翻轉；有效 target 皆為 reduce-only bid，`target_ask=None` |
| Exposure | 有效報價期間 `worst_long=0`、`worst_short=-0.00020`、position utilization `1`，未超過設定上限 |
| Quote / no-cross | 獨立稽核觀察 181 筆 status；有效 target bid 均低於 best ask，ask 永遠停用；mid、reservation price、inventory skew 與 targets 由 status 持續輸出 |
| Mutation | create attempts/success `0/0`；cancel attempts/success `0/0`；`would_place=55` 僅為模擬 |
| Reconciliation | success `361`；failure `0`；unknown orders `0`；ambiguous submission/cancellation `0` |
| REST / Exchange / WS | position/order/health polling 分別依 `3s / 10s / 60s` 設定運行；HTTP 429 `0`；WS reconnect `0`；啟動後無 WARNING/ERROR |
| Market guard | 後段 raw spread 超過 `25 bps`，正確進入 `PAUSED_MARKET` 並維持零 mutation；停止前 raw spread 約 `44.8764 bps` |
| CPU | 監看期間未見 busy loop；本輪未留存可重建的數值化 CPU 起始／峰值／結束樣本 |
| RSS / threads | 起始約 `133.8 MB / 18`，最高已保存樣本約 `136.2 MB / 26`，停止前約 `136 MB / 26`；符合已確認的 bounded executor warm-up，未見持續無上限成長 |
| 停止 | 單次 operator interrupt 後完成 cleanup；Codex Windows PTY supervisor 顯示的 exit `1` 是已由最小重現證實的 ETX harness 狀態，**不是應用程式原生 exit code**；應用 log 明確記錄正常 operator stop，無 traceback |

獨立稽核依執行中觀察判定此輪 **dry-run safety gate GO**。`PAUSED_MARKET` 是預期的安全結果，不是資料或程序錯誤；但在 raw spread 仍高於 `25 bps` 時，live 啟動條件仍為 **NO-GO**，不得為了提高 quote uptime 放寬 guard。

證據留存限制：`logs/market_maker.log` 在約 02:25 發生既有 line-limit 輪替，沒有保留 `.1`；獨立稽核已在輪替前保存前段觀察，並以後段 status 與累積 counters 銜接，但約缺少一筆 10 秒 status，且本輪沒有保存數值化 CPU 三點樣本。上述限制不改變已觀察到的 safety invariant 結論；但不得把本段當成忽略一般非零 exit 或不完整證據的通例。若後續需要符合第 5 節的完整、可重建稽核包，應在 Step B live 前重新跑滿 30 分鐘並另行保存 console/status 與 CPU/RSS 樣本。

停止後 authenticated read-only postflight：

- Market Maker 程序數 `0`。
- Account `5957` / BTC open orders `0`。
- BTC position 仍為 short `0.00020`，entry `68786.9`、cross margin、leverage `5`。
- 不需為了計算手動 flatten；恢復時必須以 authenticated REST 的實際 position 作為風控真值。若操作者另行改變倉位，恢復前重新 preflight。

### 12.4 Step C 完成時安全設定（歷史基線）

`config/market_maker/test_lighter_btc_mvp.yaml` 在交接時維持：

- `dry_run: true`
- `quote_mode: both`
- `order_size: 0.00020`
- `max_position: 0.00040`
- `base_half_spread_ticks: 4000`
- `reprice_threshold_ticks: 1000`
- `max_raw_spread_bps: 100`
- `refresh_interval_ms: 5000`
- `min_order_lifetime_ms: 60000`
- `max_mutations_per_minute: 2`
- `post_only: true`
- `startup_open_order_policy: abort`
- `unknown_order_policy: pause`
- `cancel_on_shutdown: true`

Step C validated baseline config SHA-256：`f23b0e02a1de7804e1a8818d47a5e5e3a0859286ab6cd83be93b24573cb42278`。後續尚未驗證的高換手候選設定見第 12.10 節；不得把候選值誤記為此 baseline 已通過。

Fee rate 目前以保守值 `maker_fee_rate: 0.00050`（`5 bps`）設定。2026-08-20 以 authenticated `accountLimits` 及官方 authenticated trade export 交叉驗證 account `5957`：近 7 日 BTC 的 `667/667` 筆 maker fills 均符合 `Trade Value × 0.000120`，`29/29` 筆 taker fills 均符合 `Trade Value × 0.000350`（依 CSV 顯示精度四捨五入，mismatch `0`）。因此本次可驗證的實際費率為 maker `0.000120`（`1.2 bps`）、taker `0.000350`（`3.5 bps`）；設定值 `5 bps` 是刻意保守的策略假設，不得誤記為實際收費率。Tier/profile/stake 改變或再次 live 前仍須重新核對；績效後驗應使用 export 的實際 `Fee`。

### 12.5 尚未完成

1. **Step D 小額長時間**：尚未開始。下一階段需另取得操作者授權，連續執行數小時並觀察 position drift、cancel latency、post-only cancel rate、quote uptime、fills、funding/fee 與資源使用。
2. **Step E 逐步調參**：尚未開始。只有 Step D 明確 GO 後才能一次變更一項參數，並各自重做 dry-run、live 觀察、停止與 authenticated postflight。
3. **Shutdown cancellation observation**：Step C 的兩筆 shutdown cancel 均已 reconciliation 並由 authenticated history 證明 terminal，故不是未解決事故；Step D 仍須持續記錄 acknowledgement 至 terminal proof 的延遲。任何未能精確解決的 cancellation 都立即維持事故狀態。
4. **VPS 同步與測試**：不在本次作業範圍，未執行。

### 12.6 恢復作業順序

1. 確認沒有其他手動或自動 BTC 策略，且 account/symbol 仍為獨占。
2. 確認 config 仍為 `dry_run: true`，先執行 authenticated read-only preflight，記錄 account、network、metadata、實際 position 與 BTC/account open orders `0`。
3. 重新核對 fee、funding、margin、tick/step/minimum；raw spread 必須 `<= 100 bps`，且 `$400` half-spread 算出的 targets 仍須位於 external same-side BBO 外側；book/position/WS/REST 必須健康。Account `5957` 的 2026-08-20 verified maker/taker rate 為 `1.2 / 3.5 bps`，但不得假設未來仍不變。
4. 若程式或已驗證的交易／風控參數在本交接後改動，先重跑完整離線測試與 30 分鐘 exact-config dry-run；本輪 5 分鐘 targeted calibration 不得當成後續縮短一般 gate 的先例。
5. 取得操作者對 Step D live 的新授權後，才將 live 執行所用設定明確改為 `dry_run: false`；命令必須直接在 PTY 執行，不得加 `ForEach-Object`、pipe、redirection 或其他會攔截 Ctrl+C 的包裝。
6. Step D 按第 11 節執行數小時；任何 fill、position drift、post-only cancel、ambiguous/unknown state、stale、429、資源異常或 invariant failure 都須記錄並依 gate 停止。
7. 停止後必須以 authenticated REST/交易所介面確認 BTC/account open orders `0`、實際 position，以及每筆 cancel 的 terminal proof；只有明確 GO 才能進 Step E。
8. Step E 每次只改一項參數，並重新執行相稱的 dry-run、live 觀察與 postflight。VPS 導入需另立作業範圍，不得由本節的本地 GO 自動推定。

### 12.7 Flat-account 恢復作業紀錄（2026-08-20 晚間）

操作者已先平掉上一輪殘留的 `0.00020` BTC short，並授權在安全 gate 通過時自行建立測試訂單。本輪採用「不為測試刻意製造持倉」原則：先以 flat 帳戶重新驗證既有 Step B 設定；授權不代表可在市場條件不合格時送單。

恢復前 authenticated read-only preflight：

- Profile `robinhood`、chain `466324`、account `5957`、wallet check `PASS`。
- BTC market `1` active；`price_decimals=1`、`size_decimals=5`、`min_base_amount=0.00020`、`min_quote_amount=10`。
- BTC position `0`、open orders `0`；本機沒有其他 Market Maker 程序。
- `USDG total/free=499.486907`、used `0`。
- Exact config SHA-256：`055583e133b89aed5cf39836763ffcaffb19c84fccb173c55f9ca83a4262d94d`（本輪完成後僅更新註解語意，交易與風控值未改變）。
- 完整離線測試：`371 tests / OK`（`failures=0`、`errors=0`）。測試輸出的下單／取消及 ERROR/CRITICAL 文字皆為 fake/mock error-path 情境，沒有真實交易 mutation。

Flat-account exact-config dry-run 使用檔案內 `dry_run: true` 並加 CLI `--dry-run`，執行時間為 2026-08-20 20:36:58 至 21:07:06（Asia/Taipei）：

| 項目 | 結果 |
|---|---|
| 執行時間 | `1805.30s`；單次 operator interrupt 後正常 cleanup |
| Cycles | `358 / 358` 成功，failed `0` |
| 狀態 | 獨立稽核 180 筆 status：`ACTIVE=19`、`PAUSED_MARKET=161`，invariant violations `0` |
| Position / side | 全程 position `0`；有效 target 僅為 non-reduce-only bid，`target_ask=None`；live orders 全程 `None` |
| Exposure | `worst_long=0.00020`、`worst_short=0`，未超過 one-lot Step B cap |
| Mutation / fill | create `0/0`、cancel `0/0`、fills `0`；`would_place=39` 僅為模擬 |
| Reconciliation / fault | reconciliation success `358`、failure `0`；unknown、ambiguous、HTTP 429、WS reconnect、WARNING/ERROR 均為 `0` |
| Data freshness | 最大 book age `0.094s`；最大 position age `7.422s`，均低於設定門檻 |
| Market guard | raw spread 觀察範圍 `5.88–80.46 bps`；超過 `25 bps` 後正確維持 `PAUSED_MARKET`、targets `None`、零 mutation |
| Resources | worker 起始約 `133.9 MB / 16 threads`，停止前約 `135.1 MB / 26 threads`；符合已知 bounded executor warm-up，未見 busy loop 或無界成長 |

判定分成兩層：

- **Dry-run software/safety gate：GO。** 全部軟體 invariants、fail-closed 行為、資源與正常停止均通過。
- **Step B live eligibility：NO-GO。** 最後 raw spread `80.46 bps > 25 bps`；因此本輪沒有將 `dry_run` 改為 `false`，沒有建立或取消任何真實測試訂單，也沒有進入 Step C。

停止後 authenticated read-only postflight：Market Maker 程序 `0`、BTC open orders `0`、BTC position `0`。這是當時的歷史快照；後續 spread 校準與第二次 Step B live 見第 12.8 節，最終 position 與 rollout 狀態以第 12.9 節為準。

### 12.8 配對 spread 校準與第二次 Step B live（2026-08-20 晚間）

為避免反覆以同一個 `25 bps` raw-spread gate 得到低資訊量的 NO-GO，本輪沒有只放寬 gate，而是同步把報價移到更保守的位置：

- `base_half_spread_ticks: 2000 → 4000`（BTC tick `$0.1`，即 `$200 → $400`）。
- `max_raw_spread_bps: 25 → 100`。
- `reprice_threshold_ticks=1000`、`order_size=max_position=0.00020`、`bid_only`、`POST_ONLY`、`min_order_lifetime=60s`、`max_mutations=2/min` 均不變。

在上一輪極值 midpoint `71738.6`、raw spread `80.46 bps` 下，新 target bid 為 `71338.6`，比當時 external best bid `71450.0` 低 `$111.4`；在 `100 bps` gate 邊界仍位於 same-side BBO 外側。因此這是「放寬市場可用範圍，同時把真單移遠」的配對校準，不是刪除 fail-closed guard。Lighter 官方定義 POST_ONLY crossing 時會自動取消，不能變成 taker；本地 no-cross 與 post-only-cancel recovery 仍是必要的第二層保護。

校準驗證：

- 調整後 loader `PASS`；config/strategy/Lighter integration 共 `49 tests / OK`。
- 兩分鐘 REST BBO preflight 共 13 筆：raw spread min/median/max/last 為 `0.013898 / 0.472439 / 2.751238 / 0.013903 bps`；position `0`、open orders `0`。
- Targeted exact-config dry-run：`313.937s`、`62/62` cycles、全程 `ACTIVE`；31 筆 status violations `0`。
- Dry-run raw spread `3.9047–25.3237 bps`；target bid 始終低於 best bid，最小距離 `$309`；真實 mutation/fill/fault 均為 `0`。

第二次 Step B live 於 21:29:17 啟動：

- 建立且只建立一張 `BUY 0.00020 @ 71495.6 / limit / POST_ONLY / reduce_only=false`；authenticated REST 已精確確認 active。
- 當時 BBO `71928.1 / 71932.1`，測試 bid 比 best bid 低 `$432.5`；position 仍為 `0`。
- 停止前 `9/9` cycles、reconciliation success `9`；create `1/1`，第二筆 create、fill、post-only cancel、unknown、ambiguous、429 與 reconciliation failure 均為 `0`。

停止與 postflight 判定：

- 為避免 console 顯示交易識別碼，live 命令曾被外層 PowerShell `ForEach-Object` 輸出過濾 pipeline 包裝。Ctrl+C 終止了 pipeline，沒有讓應用寫出 normal-stop marker；這是本次操作 harness 錯誤，不是已驗證的 coordinator direct-PTY shutdown 路徑。
- 21:30:31 authenticated poll 證實程序已退出但測試 bid 仍 active。準備執行精確人工 cancel 前，21:32:08 precheck 已為 active orders `0`，因此依 fail-closed 前置條件沒有送出任何人工 cancel mutation。
- Order history 隨後唯一精確證明該測試 bid 在 21:31:52 `FILLED`：filled `0.00020`、remaining `0`、POST_ONLY、reduce-only `false`。
- Fresh authenticated postflight：Market Maker 程序 `0`、BTC open orders `0`、BTC position `LONG 0.00020`。Config 已恢復 `dry_run: true`，SHA-256 為 `8106d693c06b3b7d05faec1712f17dd91ea7cdf388f0edd79236054555eef69b`。

結論：spread 配對校準與 Step B create/POST_ONLY/REST sync 本身通過，但 clean shutdown/cancel 驗證失敗，且留下已精確證明的最小 long position，故本輪 Step B 整體為 **NO-GO**。這是歷史事件；後續 position 處置、clean Step B retry 與 Step C 結果見第 12.9 節。

### 12.9 殘留倉位處置、Step B clean retry 與 Step C（2026-08-20 晚間）

操作者授權處置第 12.8 節的測試殘留倉位後，authenticated precheck 確認 BTC position `LONG 0.00020`、BTC/account open orders `0`。本輪只送出一次 `SELL 0.00020 / market / IOC / reduce-only`，採 `0.1%` execution-price bound 且不重試；submission acknowledgement 成功。Order history 證明該筆於 21:44:12 filled `0.00020`、remaining `0`，兩次 fresh authenticated confirmation 均為 position `0`、BTC/account open orders `0`。

Step B clean retry 於 21:46:04 以 direct PTY 啟動，命令沒有 pipe、redirection 或外層輸出過濾：

- 只建立一張 `BUY 0.00020 @ 71198.2 / limit / POST_ONLY / reduce_only=false`，authenticated REST 確認 active；position 維持 `0`。
- 停止前 `9/9` cycles、create `1/1`、reconciliation success `9`；第二筆 create、fill、fault 均為 `0`。
- 21:46:45 單次 operator interrupt 後，應用寫出 normal-stop marker；history 證明該單於 21:46:46 terminal `CANCELED`、filled `0`。
- 獨立 authenticated postflight 為 Market Maker 程序 `0`、BTC/account open orders `0`、BTC position `0`。

因此 Step B 的 create、POST_ONLY、active-order sync、shutdown cancel terminal 與 flat postflight 均完整通過，判定 **GO**。

Step C 使用 `quote_mode=both`、`order_size=0.00020`、`max_position=0.00040`、`base_half_spread_ticks=4000`、`max_raw_spread_bps=100`。Loader 驗證通過；config、risk manager、strategy 與 Lighter integration 共 `67 tests / OK`。Exact-config targeted dry-run 於 21:48:41.316 啟動：

| 項目 | 結果 |
|---|---|
| 執行時間／停止 | 最後 status uptime `353.813s`；21:54:39.796 單次 operator interrupt 後 normal stop |
| Cycles / 狀態 | `70/70` cycles 成功；`35/35` status 全為 `ACTIVE`；停止前至少連續 `60.029s` ACTIVE |
| Targets / exposure | `35/35` status 均同時存在 bid/ask target；最小 target gap `$800`；position `0`、worst exposure `±0.00020` |
| Market guard | raw spread `4.08–59.26 bps`，全程低於 `100 bps` gate |
| Mutation / fault | `would_place=140` 僅為模擬；真實 create/cancel/fill/fault 均為 `0`；reconciliation success `70`；WARNING/ERROR/CRITICAL `0` |
| Postflight | Market Maker 程序 `0`、BTC/account open orders `0`、BTC position `0` |

此 `5m` dry-run 是操作者因先前多輪 `30m` 重跑只反覆得到相同 market-gate NO-GO，而明確允許的本輪 targeted calibration deviation；它不修改第 11、12.6 節的一般 `30m` exact-config dry-run 規範，也不得作為後續跳過完整 gate 的通例。本輪 paired calibration 同時放寬 raw-spread eligibility 並把報價移遠，且上述 targets、exposure、no-mutation 與 fault invariants 均通過，故 targeted dry-run 判定 **GO**。

Step C live 於 21:55:44.673 啟動：

| 項目 | 結果 |
|---|---|
| 執行時間 | 最後 status 於 22:25:55，uptime `1814.203s` |
| Cycles / 狀態 | 獨立稽核 181 筆 status；`364/364` cycles 成功，全程 `ACTIVE` |
| Orders / mutation | 啟動階段只建立雙邊各一張 `0.00020` 報價；create `2/2`，運行期間 cancel `0`、fill `0` |
| Position / exposure | position 全程 `0`；worst exposure `±0.00020`，未超過 `±0.00040` cap |
| Market / no-cross | raw spread `5.99–72.13 bps`，全程低於 `100 bps` gate；`181/181` status 均同時存在 live bid/ask，no-cross violations `0`；最後 live bid/ask `71352.9 / 72151.8` |
| Faults | 最後 status 的 failed cycle、reconciliation failure、unknown order、ambiguous submission/cancellation、HTTP 429 與 WS reconnect 均為 `0` |
| Resources | worker 約 `134.8 → 135.9 MB`、threads `16 → 25`；未見 busy loop 或無界成長 |

22:25:59 收到單次 operator interrupt，應用寫出 normal-stop marker。Shutdown 對兩張報價送出的 cancel acknowledgement 各有一次短暫缺少 terminal proof 的 warning；程式沒有盲目重送，而是由 reconciliation 將兩筆確認為 complete。獨立 authenticated postflight 證明：

- Market Maker 程序 `0`、BTC/account active orders `0`、BTC position `0`。
- 本輪時間窗的 inactive history 恰有一張 BUY 與一張 SELL，皆為 `0.00020 / limit / POST_ONLY / reduce_only=false / CANCELED / filled=0 / remaining=0`。
- 因此目前沒有 unresolved cancellation、open order 或殘留 position；停機警告保留為後續長時間觀察項目，不得誤寫成從未發生 ambiguity。

依本指南第 10 節「不得有未解決 uncertainty」的判準，Step C 的雙邊 create、每側一張、POST_ONLY/no-cross、exposure cap、30 分鐘穩定性、shutdown reconciliation 與 authenticated postflight 均通過，判定 **GO（附 documented shutdown observation）**。作業在 Step C 後停止，未進入 Step D/E。Config 已恢復 `dry_run: true`，SHA-256 為 `f23b0e02a1de7804e1a8818d47a5e5e3a0859286ab6cd83be93b24573cb42278`。

### 12.10 高換手長時間測試候選（尚未驗證）

操作者準備自行執行較多成交、長時間的本地測試。為維持最小單量與既有 exposure cap，`config/market_maker/test_lighter_btc_mvp.yaml` 只調整成交機會與換價節奏：

- `base_half_spread_ticks: 4000 → 500`
- `reprice_threshold_ticks: 1000 → 250`
- `max_raw_spread_bps: 100 → 30`
- `min_order_lifetime_ms: 60000 → 30000`
- `max_mutations_per_minute: 2 → 8`

`order_size=0.00020`、`max_position=0.00040`、`quote_mode=both`、`refresh_interval_ms=5000`、保守 `maker_fee_rate=0.00050` 與所有 POST_ONLY/fail-closed/shutdown 欄位均不變；`dry_run` 維持 `true`。Candidate SHA-256：`61e28a7586021c52d26fa17860ea05ce5d7cb77769cc63609a6727f6f5e024ac`。

此候選同時調整一組互相耦合的 spread/reprice/lifetime/mutation 參數，因此視為新的 exact-config profile，**目前不是 GO，也不是第 12.4 節 baseline 的直接延續**。Live 前必須重新完成 30 分鐘 exact-config dry-run、authenticated preflight、account/BTC 獨占與 open orders `0` 確認。程式沒有日損或 PnL stop；操作者必須另行決定測試時長與最大可承擔損失。只有上述 gate 全部通過後，才可由操作者把 `dry_run` 改為 `false`；停止後須立即恢復 `true`，並驗證 BTC/account open orders `0`、每筆 cancel terminal 與實際 position。不得為追求成交再放寬 `max_raw_spread_bps`，也不得在同一輪提高 `order_size` 或 `max_position`。
