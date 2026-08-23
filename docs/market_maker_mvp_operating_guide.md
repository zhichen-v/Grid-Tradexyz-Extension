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
| `account_audit_interval_seconds` | In-process authenticated account audit 間隔；`0` 為停用，但 live 入口會拒絕停用 audit 的設定。 |
| `account_audit_timeout_seconds` | 包含等待 coordinator cycle lock 與完整 snapshot 的 audit 上限；timeout 直接 hard stop。 |
| `max_session_drawdown` | 以啟動時 total account value 為基準的 session hard stop；audit 啟用時必須大於 0。 |
| `economic_min_fills` | 達此 completed maker fill 門檻即啟用 fee/net gate；完整 equity GO 另待自然回到 flat。 |
| `min_completed_net_turnover_bps` | Completed net／雙腿 turnover 與 flat account-value change／turnover 的最低 bps。 |
| `require_flat_start` | Audit 啟用時必須為 `true`；非 flat 不得建立 session 基準。 |
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

### 5.1 風險分級 revalidation（2026-08-23 起適用）

第 5 節的 30 分鐘 gate 是完整的 T3 gate。為避免每個不影響交易語意的小改動都重跑 30 分鐘，只有在最新 Market Maker runtime、adapter、設定與帳戶組合已建立一次綠色 T3 baseline 後，才可依下表縮短後續 revalidation。Tier 依實際行為影響判定，不依 commit 名稱或修改行數判定；無法證明屬於較低層級時一律向上選擇。

| Tier | 最短連線 dry-run | 適用範圍 | 額外通過條件 |
|---|---:|---|---|
| T0 | 0 分鐘 | 純文件、註解、測試或不進入 runtime 的工具；或已證明 loader 解析結果完全相同的格式調整。 | 執行受影響的離線測試；不得改變 runtime/config fingerprint。 |
| T1 | 5 分鐘 | 純 log、metrics、redaction 等觀測性變更，且已證明不改變 branch、timing、錯誤處理、帳戶、quote、risk 或 order state。 | 至少 50 個 successful cycles、failed cycle `0`；每條受影響的週期性讀取路徑至少 3 筆樣本。 |
| T2 | 15 分鐘 | 非交易 operational plumbing，且已證明 parsed data、freshness、state transition、mutation、shutdown 語意均不變。 | 至少 150 個 successful cycles、failed cycle `0`；受影響週期性路徑至少 10 筆樣本。 |
| T3 | 30 分鐘 | 策略/定價/費率、風控/曝險、create/cancel/reprice、post-only、rate limit、submission ambiguity、history/client ID reconciliation、position/WS/REST/coordinator state、parser、adapter/SDK/profile/account/network、shutdown、任何交易設定，或事故修復。 | 完整執行第 5 節 gate；事故路徑另須有 deterministic regression test。 |

所有會執行的 Market Maker 程式改動，都先跑受影響的 targeted tests、六個 `test_market_maker_*.py` 模組、相關 Lighter/preflight/shutdown safety slice，再跑完整 `unittest discover`。T1 至 T3 開始前均須保存 code diff/commit、exact-config SHA-256、SDK/network/profile、read-only signer/wallet ownership、帳戶與 BTC 獨占、程序數 `0`、open orders `0`、實際 position/balance、metadata/fee/funding/margin；dry-run 必須同時保留檔案 `dry_run: true` 與 CLI `--dry-run`。停止後再確認程序 `0`、account/BTC open orders `0`、position 與 fill 數未變、真實 create/cancel/cancel-all `0`，並保存 log 與 CPU/RSS 樣本。

Gate 綁定最後候選的 code/config/account fingerprint；可先合併同一批小修改，最後只跑一次相稱 gate，不必每個 commit 各跑一次。任一 safety invariant、reconciliation、unknown/uncertainty、mutation、正常停止或 postflight 證據失敗，都判定 **NO-GO**；修正後回到 T3。不得刻意在 Robinhood 主網製造斷網或不確定送單來驗證事故路徑，該路徑使用 deterministic fake/Mock 測試。

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

## 12. Phase 8 本地驗證紀錄與交接（2026-08-20 起）

> **目前狀態：STOPPED / 第 12.16 節首次 E1 live 的歷史判定仍為 safety NO-GO／economic INCONCLUSIVE；第 12.17 節已完成 in-process audit 與 long-run 候選修復，新的 live proof 尚待操作者執行。** 歷史 `SHORT 0.00040 BTC` 已由操作者處置；是否仍 flat、account orders 是否仍為 0 一律以每次啟動前的 fresh authenticated preflight 為準，不得沿用文件快照。Config 維持 `dry_run: true`；VPS 同步與測試仍不在本階段範圍。

### 12.1 本輪完成項目

- 2026-08-20 當時完成 Market Maker 專項與完整離線測試，結果為 `371 tests / OK`；2026-08-23 現行基線與差異歸因見第 12.11 節，不得把此歷史數字誤認為最新 full-suite 結果。
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

Step C validated baseline config SHA-256：`f23b0e02a1de7804e1a8818d47a5e5e3a0859286ab6cd83be93b24573cb42278`。後續高換手候選的 dry-run 與首次 live 結果見第 12.10–12.12 節；不得把候選值誤記為此 baseline 已通過。

Step C baseline 當時以保守值 `maker_fee_rate: 0.00050`（`5 bps`）設定。2026-08-20 以 authenticated `accountLimits` 及官方 authenticated trade export 交叉驗證當時帳戶：近 7 日 BTC 的 `667/667` 筆 maker fills 均符合 `Trade Value × 0.000120`，`29/29` 筆 taker fills 均符合 `Trade Value × 0.000350`（依 CSV 顯示精度四捨五入，mismatch `0`）。因此本次可驗證的實際費率為 maker `0.000120`（`1.2 bps`）、taker `0.000350`（`3.5 bps`）；設定值 `5 bps` 是刻意保守的歷史策略假設，不得誤記為實際收費率。Tier/profile/stake 改變或再次 live 前仍須重新核對；目前 Step E fee-truth 校準與精確費用算法見第 12.16 節。

### 12.5 尚未完成

1. **Step D 小額長時間**：首次 live 於約 `5m15s`、修復後 retry 於約 `28m55s` 均提前停止並判定 **NO-GO**，仍未完成原授權的 `2h` 長時間 gate。第 12.15 節已完成 cancellation-only bounded confirmation 修復、離線驗證與 T3 dry-run；尚待 fresh live 授權及完整 Step D。
2. **Step E 逐步調參**：操作者於 2026-08-23 明確接受跳過 Step D prerequisite 後已開始；E0 fee-truth 與 E1 spread T3 已完成。首次 E1 live 最後由 operator interrupt 停止，但只取得兩筆開倉 maker fills 且留下 `-0.00040 BTC`，另有監看讀值不一致與 hard-stop propagation 缺口，因此 economic result 為 INCONCLUSIVE，E2 未開始。這是 operator-accepted rollout deviation，不會把 Step D 歷史 NO-GO 改判為 GO；每一階仍只能改一項，並各自保存 fingerprint、驗證、停止與 authenticated postflight。
3. **Live proof**：第 12.14 節自然驗證 position-cap 下只報 reduce-only side 並正常回到 flat，但當時累積 mutation 尚低於 `8/min`，所以沒有觸發 emergency budget；該額外額度仍只有 deterministic regression proof。新的 cancellation-only `4 / 0.5s` window 亦只有 deterministic 與 dry-run proof，仍須在下一個 live stage 自然發生取消時觀察；不得在主網刻意製造 ambiguity 或 budget exhaustion，也不得用 blind signer retry 掩蓋 read-after-write timing。
4. **VPS 同步與測試**：不在本次作業範圍，未執行。

### 12.6 恢復作業順序

以下是回到 Step D 路線時的原恢復程序。操作者於 2026-08-23 已明確改走第 12.16 節的 Step E staged calibration；該決策只豁免「Step D GO 後才進 Step E」的 prerequisite，不豁免定價/config fingerprint 的相稱 revalidation、live risk gate 或停止後 authenticated proof。

1. 確認 live candidate 仍是第 12.15 節已 push 的 commit，working tree 沒有 runtime/config 變更；若候選改變，依第 5.1 節重新判定 revalidation tier，不能沿用本輪 T3。
2. Live 前重新確認沒有其他手動或自動 BTC 策略，測試 subaccount/symbol 仍為獨占；config 必須先維持 `dry_run: true`，並以 fresh authenticated read-only preflight 驗證 network/wallet ownership、metadata、fee/funding、`1x cross`、權益、實際 position 與 BTC/account open orders `0`。
3. 沿用獨立 account/risk 與 runtime/resource 稽核，但 read-only monitor 必須把單次 DNS/read fault 記錄後做 bounded recovery；重複失敗、無法取得可信帳戶狀態或 hard-stop evaluator 中斷時，live 立即停止，不能只讓 monitor 靜默退出。
4. 取得操作者對 Step D live retry 的 **fresh 明確授權**後，才將執行用設定改為 `dry_run: false`；主程序以 direct PTY 啟動，不加 pipe、redirection 或會攔截 Ctrl+C 的包裝，並依原 `2h / 5 USDG / 1x cross` 邊界執行。
5. 任何 ambiguous/unknown、loss gate、repeated stale/429、資源或 invariant fault 都立即停止；新的 cancellation window 只能等待自然 cancel 驗證，不得為測試刻意製造 uncertainty。
6. 停止後必須以至少兩次 fresh authenticated read 確認 BTC/account open orders `0`、實際 position，以及每筆 uncertain cancel 的 exact terminal proof；config 立即恢復 `dry_run: true`。只有完整 Step D 明確 GO 才能進 Step E。
7. Step E 每次只改一項參數，並重新執行相稱的 dry-run、live 觀察與 postflight。VPS 導入需另立作業範圍，不得由本節的本地 GO 自動推定。

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

### 12.10 高換手長時間測試候選（dry-run GO；首次 live NO-GO）

操作者準備自行執行較多成交、長時間的本地測試。為維持最小單量與既有 exposure cap，`config/market_maker/test_lighter_btc_mvp.yaml` 只調整成交機會與換價節奏：

- `base_half_spread_ticks: 4000 → 500`
- `reprice_threshold_ticks: 1000 → 250`
- `max_raw_spread_bps: 100 → 30`
- `min_order_lifetime_ms: 60000 → 30000`
- `max_mutations_per_minute: 2 → 8`

`order_size=0.00020`、`max_position=0.00040`、`quote_mode=both`、`refresh_interval_ms=5000`、保守 `maker_fee_rate=0.00050` 與所有 POST_ONLY/fail-closed/shutdown 欄位均不變；`dry_run` 維持 `true`。2026-08-23 最新 exact-config SHA-256：`5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`。

此候選同時調整一組互相耦合的 spread/reprice/lifetime/mutation 參數，因此視為新的 exact-config profile，不是第 12.4 節 baseline 的直接延續。第 12.11 節已完成其修復版 30 分鐘 T3 dry-run、authenticated preflight/postflight、帳戶獨占與零 mutation 驗證，故 **dry-run software/safety gate 為 GO**；這不代表 live GO。第 12.12 節的首次 live 已因 cancellation ambiguity 與 risk-reduction mutation-limiter observation 提前停止並判定 NO-GO；修復、T3 revalidation 與 fresh live 授權前不得重啟。不得為追求成交再放寬 `max_raw_spread_bps`，也不得在同一輪提高 `order_size` 或 `max_position`。

### 12.11 Fast-fill uncertainty 修復、測試子帳戶切換與 T3 revalidation（2026-08-23）

#### 事故與根因邊界

2026-08-20 23:37 的高換手測試中，一張最小量 `SELL 0.00020` 在 submission 後快速成交；active-order 查詢當下已找不到該單，submission resolver 因而無法立即證明結果。系統正確 fail-closed 進入 `PAUSED_ORDER_STATE`，沒有在不確定狀態下盲目補單，但暴露兩個 Market Maker 問題：submission uncertainty latch 在 exact reconciliation 已解決後仍可能維持到程序結束，以及 paused status 可能沿用先前的 position/live-order 快照。當時 status 為 `8/9` successful cycles、`ambiguous_submissions=1`、`reconciliation_failure=1`；這是 **NO-GO 事故紀錄**，不能以「沒有重複下單」直接視為通過。

後續 grid/shared adapter 更新已在目前基線提供「active lookup miss 後，以 exact client order ID 查 inactive history」的 resolver fallback。本輪沒有修改 grid 或 shared Lighter adapter；只在 Market Maker 邊界完成以下最小修復與回歸證明：

- `MarketMakerOrderManager` 只有在 adapter unresolved registry 與所有 MM slots 都不再含 submission uncertainty 時，才清除 latch 與對應 pause；任何 unresolved cancel、錯誤／缺少 client ID 仍維持 fail-closed。
- Exact active/history reconciliation 證明 `OPEN/PARTIAL` 時採納同一張單；證明 `FILLED` 時先強制刷新 position，且同一 cycle 不建立 replacement，避免快速成交後重複下單。
- `MarketMakerCoordinator.emit_status_once()` 在輸出前重新讀取 book、position 與 live-order snapshot，使 paused 狀態也反映當下真值。
- 新增 fast-fill active-miss → exact inactive-history、OPEN、PARTIAL、FILLED、independent uncertain-cancel 與 paused-status freshness 回歸測試。

#### 子帳戶與離線驗證

本機 Lighter YAML 中原本會覆蓋 `.env` 的舊 account/API-key index 已改為 `null`，使兩者一致由測試子帳戶環境變數載入；識別碼與 key 不寫入本文件。`config/exchanges/lighter_config.yaml` 與 `config/market_maker/test_lighter_btc_mvp.yaml` 都受 `.gitignore` 保護，故這些本機 account/config 值不會隨 branch commit/push。Authenticated read-only preflight 通過 Robinhood mainnet profile、signer/wallet ownership、BTC metadata 與帳戶獨占：USDG total/free `300 / 300`、used `0`、positions `0`、BTC open orders `0`，且沒有 mutation。初始化仍會輸出既存的 `SymbolConversionService.get_instance` cache warning，但 authenticated reads 全部成功；依本輪「不碰 grid/shared 除非致命衝突」的範圍只列為後續維護項。

離線結果：

- 六個 Market Maker 模組：`178 tests / OK`。
- 相關 Lighter/preflight/shutdown safety slice：`34 tests / OK`。
- `compileall` 與 `git diff --check`：PASS。
- 完整 `unittest discover`：`401 tests`，其中 `8 failures + 4 errors`；在乾淨的基準 commit `2de97c4` 重跑相同模組得到完全相同的 `test_lighter_authenticated_queries: 0F/1E`、`test_lighter_grid_lifecycle: 8F/3E`。因此這 12 筆是後續 grid selective-cancel 基線問題，不是本輪 Market Maker regression；本輪未修改任何 grid 程式碼。

#### Exact-config 30 分鐘 dry-run

因本輪改動涉及 submission ambiguity/order lifecycle，依第 5.1 節歸類為 T3，仍執行完整 30 分鐘；這一輪建立綠色基準後，後續小改動才可依 T0–T2 縮短。使用第 12.10 節設定，檔案 `dry_run: true` 且 CLI 加 `--dry-run`，觀察期間為 2026-08-23 18:23:35 至 18:54:05（Asia/Taipei）。

| 項目 | 結果 |
|---|---|
| Exact config | SHA-256 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1` |
| 執行時間／停止 | 最後 status uptime `1823.062s`；單次 operator interrupt 後取消訂閱、斷線並寫出 normal operator-stop marker；程序數 `0` |
| Cycles / 狀態 | `359/359` 成功、failed `0`、consecutive errors `0`；最後為 `ACTIVE`，pause reason `None` |
| Position / exposure | position `0`、live orders `0`；worst exposure `±0.00020`，max-position utilization `0.5`，未超過 `±0.00040` cap |
| Market / quote | 最後 raw spread `28.82197 bps < 30 bps` guard；雙邊 target 存在，quote spread 約 `13.00630 bps` |
| Mutation / fill | `would_place=718` 僅為模擬；create/cancel attempts、partial/full fills 全部 `0` |
| Faults | ambiguous submission/cancellation、reconciliation failure、unknown orders、HTTP 429、WS reconnect 全部 `0`；reconciliation success `359` |
| Resources | 樣本 RSS 約 `131.99 → 133.84 MiB`；threads `16 → 25`、保存峰值 `26`；CPU `4.5 → 36.734s`，未見 busy loop 或無界成長 |

停止後 authenticated read-only postflight：USDG total/free 仍為 `300 / 300`、used `0`，BTC positions `0`、open orders `0`，Market Maker 程序 `0`。因此本輪判定 **修復版 dry-run software/safety gate GO**。本節所述修復已以 commit `1d3cd85172f4d43acdc8f1583b7be0d08f4c5351` push 至 `feat/lighter-market-maker-mvp`；後續高換手 live 的獨立結果見第 12.12 節，不能以本節 dry-run GO 覆蓋其 NO-GO。

### 12.12 Step D 高換手 live 提前停止：NO-GO 與交接（2026-08-23）

#### 授權、版本與 live preflight

操作者採用的本地 Step D 風險邊界為：最長 `2h`、相對啟動基準最大損失 `5 USDG`、BTC `1x cross`；發生停止條件時授權取消 Market Maker 訂單，並在 exact authenticated evidence 證明殘留倉位後，以最小必要的 reduce-only IOC 處置。Step E 只有 Step D 明確 GO 後才可開始，VPS 不在本作業範圍。

- Branch `feat/lighter-market-maker-mvp`；live 前 commit/push 為 `1d3cd85172f4d43acdc8f1583b7be0d08f4c5351`。
- Dry-run config SHA-256 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`；只將 `dry_run` 切為 `false` 後的 live SHA-256 為 `940bf2365114f64e5ebbf0e10a737b9437c51272c2e57e5dcfed6159f80ef68c`。
- Live safety gate：`quote_mode=both`、`order_size=0.00020`、`max_position=0.00040`、`post_only=true`、`exclusive_symbol_control=true`、startup abort、unknown pause、shutdown cancel 與 `max_raw_spread_bps=30` 均通過 loader/explicit check。
- Authenticated preflight：Robinhood mainnet、signer/wallet ownership PASS；測試 subaccount USDG total/free `300 / 300`、used `0`，BTC position `0`、BTC/account open orders `0`、Market Maker 程序 `0`。因 adapter 的一般 leverage/margin setter 尚未實作，本輪依授權只送出一次官方 signer leverage update，回應成功後 authenticated snapshot 精確確認 BTC `1x cross`；沒有重試。

#### 實際 live 觀察

Live 於 19:14:37 以 direct PTY 啟動；startup authenticated REST 精確證明雙邊各只有一張 `0.00020 / limit / POST_ONLY / reduce_only=false` 報價。約 `5m15s` 後因安全 gate 提前停止，沒有繼續等待 `2h`：

| 項目 | 最後／累積結果 |
|---|---|
| Runtime | 最後 status uptime `314.688s`；state `risk_reduction` |
| Cycles | `73` cycles、`72` successful、`1` failed |
| Create / cancel | create `10/10`；cancel `5/4` |
| Fills / position | full fills `4`；最後為 `SHORT 0.00040`，已到 configured position cap |
| Uncertainty | ambiguous submissions `0`；ambiguous cancellations `1`；最後 reconciliation failure `0`、unknown orders `0` |
| Mutation / transport | mutation-limiter blocks `7`；HTTP 429 `0`；WS reconnect `0` |

一次 cancel acknowledgement 在該 cycle 的 bounded confirmation 內沒有立即取得 terminal proof，造成一個 failed cycle 並增加 `ambiguous_cancellations`。之後可以 reconciliation，且沒有盲目 replacement，但第 11 節要求 unknown/uncertain order 出現即停止，因此該事件本身已足以判定本輪不能 GO。另在 position 到達最大 short、runtime 已進入 `risk_reduction` 時，一般 mutation limiter 曾阻擋／延遲 reduce-only bid 建立共 `7` 次；稍後雖建立一張 reduce-only bid，仍顯示減風險 mutation 的優先權需進一步稽核。這裡只記錄 observation，尚未在未完成 code/log audit 前宣稱根因。

#### 停止、reconciliation 與倉位處置

單次 operator interrupt 後，應用完成 unsubscribe/disconnect 並寫出 normal operator-stop marker；shutdown 對最後一張 reduce-only bid 的 cancel 取得 exact reconciliation proof。獨立 authenticated check 證明 Market Maker 程序 `0`、BTC/account active orders `0`，但 position 為 `SHORT 0.00040`，因此沒有把「程序已停」誤記為帳戶已 flat。

依既有授權，人工處置前再次精確確認：程序 `0`、BTC/account orders `0`、BTC `SHORT 0.00040 / 1x cross`。本輪只送出一次 `BUY 0.00040 / market / IOC / reduce-only`，以當下 best ask 計算最多 `0.1%` 的 execution-price bound（實際 bound `77416.5`），並把 adapter 額外 slippage 設為 `0`，沒有 retry。Submission acknowledgement 成功；exact client inactive history 證明 status `FILLED`、side `BUY`、filled `0.00040`、remaining `0`。其後兩次 fresh authenticated confirmation，以及恢復 config 後的另外兩次 audit，皆為 BTC position `0`、BTC/account open orders `0`。

最終 USDG total/free `299.854529`、used `0`；相對 `300` 基準的總損失／drawdown 為 `0.145471 USDG`，遠低於授權上限 `5 USDG`。Market Maker 與獨立監控程序均為 `0`。Config 已立即恢復 `dry_run: true`，SHA-256 回到 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`。

#### 判定與未完成事項

本輪 Step D 判定 **NO-GO / STOPPED EARLY**；Step E **NOT STARTED**。下一輪不得直接重啟 live，須先完成以下項目：

1. 限定在 Market Maker 範圍內稽核 cancellation bounded-confirmation 路徑，以及 `risk_reduction` 下 risk-reducing create 與一般 mutation limiter 的優先權；沒有致命衝突不得修改 grid 程式碼。
2. 為取消確認 timing 與 max-inventory risk-reduction priority 新增 deterministic regression tests，完成相關離線 suite。
3. 事故修復按 T3 重新跑完整 `30m` exact-config dry-run、authenticated flat postflight，再取得 fresh Step D live 授權。
4. Step D 未重新取得 GO 前不得進入 Step E；VPS 仍未同步、未測試。

上述第 1–3 項的本地修復與 revalidation 已完成，結果見第 12.13 節；本節首次 live 的 NO-GO 歷史判定不變。Fresh live 授權、修復後完整 Step D 與 Step E 仍未完成。

### 12.13 Step D NO-GO 修復與第二輪 T3 revalidation（2026-08-23）

#### 根因與修復邊界

針對第 12.12 節唯一一次 cancellation ambiguity，事後以該筆訂單的 exact authenticated history 稽核：交易所已於 `19:18:39` 將其標記為 `CANCELED / filled=0 / remaining=0`，本地 adapter 則在 `19:18:40.574` 才記錄 cancellation outcome uncertain。這證明該事件是 terminal cancel 已發生後的短暫 false-positive，不是真正未解決的 live order；但原輪已有 failed cycle，NO-GO 判定不變。

根因是 shared Lighter cancellation resolver 在第一個 active-order snapshot 仍看到 stale target 時立即回傳 unresolved，因而跳過同一輪 inactive history 與後續既有 bounded attempt。最小修復保留 signer mutation「只送一次」與 fail-closed 語意：active match 只記錄觀察結果，不再提前返回；每個 bounded attempt 都繼續查 exact inactive history，只有 exact cancellation-terminal status（`CANCELED/CANCELLED/REJECTED/EXPIRED`）才回傳成功。`FILLED` 不會被偽裝成 cancel success，沒有 cancellation-terminal proof 時仍回傳不確定。

第 12.12 節另一路徑的根因是 safety cancel 可以越過一般 mutation budget，但仍會記錄該次 mutation；緊接著唯一能降風險的 reduce-only create 又被同一個一般 budget 擋住。Market Maker order manager 現在只在 runtime 已是 `RISK_REDUCTION` 且 desired order 明確為 `reduce_only` 時，允許一般 budget 之外最多一筆 emergency create；此額外額度每 `60s` 最多一次，在 await adapter 前即消耗 cooldown，且仍記錄一般 mutation。普通報價、非 reduce-only 訂單、非 risk-reduction runtime 都不能使用；ambiguous/failed attempt 也不會立刻重送。

沒有修改 grid production code。Shared Lighter adapter 保留既有 `2` 次、間隔 `0.25s` 的 bounded window；對舊 stale-active 提前返回路徑，read-only 查詢由 `1 open + 0 history` 最多變為 `2 open + 2 history`，不增加 signer mutation。`tests/test_lighter_grid_lifecycle.py` 只更新舊測試對 authenticated history read 次數的預期。其餘 production 變更限於 Market Maker order manager。本節修復已以 commit `6ba1cd6e070e5bffd3dbf73aea86f9fb305e75a7` push 至 `feat/lighter-market-maker-mvp`，local/remote SHA 已精確一致；修復後 live retry 的獨立結果見第 12.14 節。

#### Deterministic regression 與離線結果

新增的兩個事故回歸在修復前都能 deterministic 失敗，修復後通過：

- Cancellation：第一輪 active snapshot 有 target、history 空；第二輪 active 已空、exact history 為 `CANCELED`。必須完成兩輪 read 並回傳成功，且不能重送 signer mutation。
- Risk reduction：先耗盡一般 mutation budget，再執行 hard-risk safety cancel；後續 reduce-only emergency create 必須成功，而同一 `60s` 內第二次 emergency create 必須被擋。

離線驗證結果：

- 六個 Market Maker 模組：`179 tests / OK`。
- Lighter mutation ambiguity slice：`20 tests / OK`。
- Authenticated query 與 market-close safety slice：`21 tests / OK`。
- `compileall`、`git diff --check`：PASS。
- 完整 `unittest discover`：`403 tests`，其中 `8 failures + 4 errors`；失敗分布仍精確等同既有基線的 `test_lighter_authenticated_queries: 0F/1E` 與 `test_lighter_grid_lifecycle: 8F/3E` selective-cancel extension incompatibility。新增兩個測試使總數由 `401` 增至 `403`，沒有新增失敗類別；本輪未修改 grid production code。

#### Exact-config 30 分鐘 dry-run

事故修復依第 5.1 節歸類為 T3。啟動前確認 Market Maker 程序 `0`，authenticated Robinhood mainnet profile、signer/wallet ownership、BTC metadata 均 PASS；positions `0`、BTC/account open orders `0`，USDG total/free `299.854529 / 299.854529`、used `0`。使用第 12.10 節 exact config，檔案保留 `dry_run: true` 且 CLI 同時加 `--dry-run`，SHA-256 為 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`。觀察期間為 2026-08-23 19:50:13 至 20:21:17（Asia/Taipei）。

| 項目 | 結果 |
|---|---|
| 執行時間／停止 | 最後 status uptime `1864.5s`；單次 operator interrupt 後 console 記錄取消訂閱、Lighter disconnect 與 normal operator-stop marker；程序數 `0` |
| Cycles / 狀態 | `367/367` 成功、failed `0`、consecutive errors `0`；前段正常 `ACTIVE`，raw spread 超過 `30 bps` 後預期進入 `PAUSED_MARKET`，累積 state transitions `3` |
| Position / exposure | position 全程 `0`；worst exposure `±0.00020`，max-position utilization `0.5`，未超過 `±0.00040` cap；最後 live bid/ask 皆為空 |
| Market / quote | 最後 raw spread `50.03673 bps > 30 bps`，因此安全停止 targets/create；暫停前累積 `would_place=382`，僅為模擬 |
| Mutation / fill | 真實 create/cancel attempts、post-only cancellations、partial/full fills 全部 `0` |
| Faults | ambiguous submission/cancellation、reconciliation failure、unknown orders、mutation-limiter blocks、HTTP 429、WS reconnect 全部 `0`；reconciliation success `367` |
| Resources | 保存樣本的 worker RSS 約 `132.77 → 133.91 MiB`、threads `14 → 22`、CPU `6.78 → 43.06s`；未見 busy loop 或持續無界成長 |

`logs/market_maker.log` 的既有 `1000` 行 line-limit 在本輪接近結束時整檔 reset，停止後只保留最後 `8` 筆 status 與 operator-stop line；因此無法從單一檔案重建全期間精確 state 樣本分布或 raw-spread min/max。獨立即時 checkpoint 已持續確認前後段 counters 與狀態，最終累積 counters 完整且全為安全結果，故本輪 T3 判定不受影響；但 Step D 長時間 live 必須另行保存不包裝主 PTY 的 read-only checkpoint/console 稽核紀錄，不能只依賴此 line-limited 檔案。

停止後連續兩次 fresh authenticated read-only postflight 均通過：Robinhood mainnet 與 wallet ownership PASS、positions `0`、BTC/account open orders `0`、USDG total/free `299.854529 / 299.854529`、used `0`。Config 仍為 `dry_run: true` 且 SHA-256 未變。因此本輪判定 **事故修復與 T3 dry-run software/safety gate GO**；這只解除第 12.12 節的本地修復/revalidation blocker，不會覆蓋首次 Step D live 的 NO-GO，也不等於修復後 live GO。Patch 後續已 review/commit/push，並依 fresh 授權執行 live retry；該輪仍為 NO-GO，見第 12.14 節。Step D 完整 GO 前不得開始 Step E，VPS 仍不在本階段範圍。

### 12.14 Step D 修復後 live retry 提前停止：NO-GO（2026-08-23）

#### 授權、版本與 preflight

操作者授權以原邊界重跑：最長 `2h`、相對 live 啟動基準最大損失 `5 USDG`、BTC `1x cross`。執行版本為已 push 的 commit `6ba1cd6e070e5bffd3dbf73aea86f9fb305e75a7`；config 的 dry/live SHA-256 分別為 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1` 與 `940bf2365114f64e5ebbf0e10a737b9437c51272c2e57e5dcfed6159f80ef68c`，兩者只差 `dry_run`。

Authenticated preflight 通過 Robinhood mainnet、signer/wallet ownership、market metadata 與帳戶獨占；Market Maker 程序 `0`、BTC/account open orders `0`、BTC position `0`，BTC 精確為 `1x cross`，USDG total/free `299.854529 / 299.854529`、used `0`。當時 raw BBO spread 約 `1.4515 bps < 30 bps`，market active；沒有送出 preflight mutation。

為避免 line-limited 主 log reset 遺失長時間證據，live 主程序維持 direct PTY，另啟兩個完全 read-only、可獨立停止的監控：

- Account/risk audit：`logs/step_d_audit/risk-20260823T124133Z.jsonl`，SHA-256 `2cf197b43ab6f97fff4ff2b52572500168aeea9f2cecb685bbe62df602d11536`。
- Runtime/log/resource audit：`logs/step_d_audit/runtime-20260823-204234.log`，SHA-256 `ed724ec9345b6ac0fe745527f5f488b380ad98fc28df74fd122b0b40df2b3056`。

兩份 audit 均受 ignore 保護，不納入 branch；沒有記錄 key 或 account ID。Runtime audit 為保留 order-lifecycle 原始證據而含本輪訂單識別碼，因此只留本機，文件與交接摘要不得複製其值。

#### Live 結果與 risk-reduction 證據

Live 於 20:43:56 以 direct PTY 啟動；21:12:55 在第二次 cancellation uncertainty 後依 gate 提前停止：

| 項目 | 最後／累積結果 |
|---|---|
| Runtime / cycles | 最後 status uptime `1734.797s`（約 `28m55s`）；`355` cycles、`353` successful、`2` failed |
| 狀態樣本 | `ACTIVE=67`、`RISK_REDUCTION=99`、`SYNCING=4`、`PAUSED_DATA=3`；三筆短暫 stale-book 樣本自行恢復，不是本輪停止原因 |
| Create / cancel | create `22/22`；cancel `11/9`；post-only cancellations `7` |
| Fills / position | full fills `4`；position 觀察路徑為 `0 → LONG 0.00020 → LONG 0.00040 → LONG 0.00020 → 0`；最大 utilization `1`，未超過 `0.00040` cap |
| Market | 保存 status 的 raw spread 約 `1.5648–24.7742 bps`，全程低於 `30 bps` guard |
| Fault counters | ambiguous submissions `0`、ambiguous cancellations `2`、unknown orders `0`、reconciliation failures `0`、HTTP 429 `0`、WS reconnect `0` |
| Resources | worker RSS 約 `132.58–135.05 MiB`、threads `14–24`、CPU 累積約 `1.469 → 29.5s`；未見 busy loop 或無界成長 |

Position 首次到達 `LONG 0.00040` cap 後，runtime 正確維持 `RISK_REDUCTION`，只保留／建立 `SELL 0.00020 / reduce-only`；該狀態下 `mutation_limiter_blocks` 始終為 `0`，後續成交把 position 依序降回 `0.00020` 與 `0`。不過進入 risk reduction 並建立該 reduce-only order 時，累積 create/cancel mutation 僅為 `5 + 2 = 7`，仍低於 `8/min` normal budget；因此本輪只證明 risk-reduction side/reduce-only 行為正常，**不能**宣稱 emergency budget 已被 live 觸發，該額外額度仍只有 deterministic regression proof。最終 `mutation_limiter_blocks=32` 全部發生於回到 flat `ACTIVE` 後的一般雙邊換價，符合 normal budget，不能誤歸因為 risk-reduction regression。

#### Cancellation NO-GO 根因邊界

本輪有 `9` 次 cancellation acknowledgement 經既有 resolver 成功確認 complete，但另有兩個不同 target 在 `2 attempts / 0.25s` window 內沒有取得 terminal proof；兩者各只增加一次 cancel mutation attempt，沒有 signer 重送，也沒有 replacement、429、read exception、fill race 或 history pagination 排除。這兩次造成 `2` failed cycles 與 `ambiguous_cancellations=2`，已直接符合停止條件。

停止後以 exact authenticated inactive history 反查，兩筆都唯一為 `CANCELED / filled=0 / remaining=0`；交易所 terminal timestamp 分別約早於本地 uncertainty error `1.54s` 與 `1.32s`。這證明帳本最終沒有未解決訂單，但不證明 terminal endpoint 在 bounded snapshots 當下已可見；根因邊界是偶發 cancel-terminal visibility/propagation lag 超過現行 read window。第 12.13 節的 stale-active early-return 修復有效但不充分，不能把本輪改判為 GO。

下一個最小修復是 cancellation-only bounded read window（建議先驗證 `4 attempts / 0.5s`），不放寬 submission resolver、不重送 signer mutation、不接受非 cancellation-terminal status。必須先以「第三輪才看見 exact `CANCELED`」的 deterministic test 與 window-exhaust test 證明，再依第 12.6 節重做完整 T3 與 fresh live authorization。

#### 停止與 postflight

單次 operator interrupt 後，應用完成取消訂閱、Lighter disconnect 並寫出 normal operator-stop marker；主程序與兩個獨立監控皆已停止。Risk audit 共 `132` 個 snapshots，權益 min/max 為 `299.840322 / 299.886275`；相對啟動基準的最大觀察損失 `0.014207 USDG`，遠低於 `5 USDG` hard stop，且沒有 monitor fault。

停止後兩次 fresh authenticated read 均證明 BTC position `0`、BTC/account open orders `0`、USDG total/free `299.882680 / 299.882680`、used `0`；相對 live 啟動基準為 `+0.028151 USDG`。因此不需要也沒有送出人工 reduce-only IOC。Config 已立即恢復 `dry_run: true`，SHA-256 回到 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`。

本輪 Step D 判定 **NO-GO / STOPPED EARLY**，不是帳戶殘留風險事故；Step E **NOT STARTED**。作業依先前指示在 NO-GO 後暫停，未修改 grid production code，亦未進行 VPS 同步或測試。

### 12.15 Cancellation visibility window 修復與第三輪 T3 revalidation（2026-08-23）

#### 最小修復與版本

第 12.14 節兩次 cancellation uncertainty 都是成功 signer acknowledgement 後，exact inactive-history terminal visibility 超出既有 `2 attempts / 0.25s`；沒有 signer 重送、fill race、429、read exception 或 pagination 排除。後續修復只在 shared Lighter REST adapter 新增 cancellation 專用 `CANCELLATION_RECONCILIATION_ATTEMPTS=4` 與 `CANCELLATION_RECONCILIATION_DELAY=0.5`，並只讓 `_reconcile_cancellation()` 使用。Submission/bulk unresolved path 仍維持 `MUTATION_RECONCILIATION_ATTEMPTS=2`、`MUTATION_RECONCILIATION_DELAY=0.25`；每筆 cancel signer mutation 仍只能送一次，後續最多四輪全是 read-only open/history 查詢，只有 exact `CANCELED/CANCELLED/REJECTED/EXPIRED` 才成功，`FILLED` 不會偽裝為 cancel success。

上一輪 Step D NO-GO 交接文件已先獨立以 commit `7a66ea718a5ae73a006b09201a935ad9bff73c25` push。上述 functional fix 與 tests 已以 commit `d1cd2f4dc48198b9f3f0aeee0f70babd12c8aa91` push 至 `feat/lighter-market-maker-mvp`，local/remote SHA 精確一致。Production diff 只有 shared `lighter_rest.py`；沒有修改 grid production code 或 Market Maker strategy/risk/order-manager 程式。

#### Deterministic red/green 與離線驗證

兩個事故測試在舊 `2 / 0.25s` implementation 下先得到預期的 `2 failures`，修復後轉為 `3/3 OK`：

- 成功 cancellation acknowledgement 後，前兩輪 history 為空、第三輪才出現 exact `CANCELED`；結果必須成功，open/history 各讀三次、sleep 兩次，signer cancel 精確一次。
- Window 全部耗盡後再次呼叫同一 target；兩次呼叫合計 open/history 各八次、sleep 六次，兩次都保持 unresolved/fail-closed，signer cancel 仍精確一次。
- 既有 stale-active → 第二輪 exact history 測試改用 cancellation 專用 override，仍通過。

完整離線結果：

- 六個 Market Maker 模組：`179 tests / OK`。
- Lighter mutation ambiguity + authenticated query/market-close safety slice：`41 tests / OK`。
- `compileall`、`git diff --check`、新增 secret-assignment audit：PASS。
- 完整 `unittest discover`：`403 tests`，仍為既有 `8 failures + 4 errors`；分布精確維持 `test_lighter_authenticated_queries: 0F/1E`、`test_lighter_grid_lifecycle: 8F/3E` selective-cancel extension baseline，沒有新增失敗類別。

#### Exact-config 30 分鐘 T3 dry-run

T3 啟動前 candidate/config/account fingerprint：commit `d1cd2f4dc48198b9f3f0aeee0f70babd12c8aa91`；config SHA-256 `5e03d60a07f25baf4cbf267f7afc1a319edefea0dc85baffde6ef73ab880bac1`，檔案 `dry_run: true` 且 CLI 加 `--dry-run`。Loader 精確確認 `both / 0.00020 / 0.00040 / half-spread 500 ticks / raw guard 30 bps / reprice 250 ticks / refresh 5s / lifetime 30s / mutations 8/min / POST_ONLY / exclusive / abort / pause / shutdown cancel`。Authenticated preflight 為 Robinhood mainnet/wallet PASS、程序 `0`、BTC/account orders `0`、BTC position `0`、`1x cross`、USDG total/free `299.882680 / 299.882680`、used `0`；BTC history baseline `33`。Account limits 為 maker/taker fee tick `120/350`、tier `premium`，BTC funding snapshot `0.0001`，啟動前 raw BBO spread約 `1.4446 bps < 30 bps`。

Dry-run 以 direct PTY 於 21:57:44 啟動、22:28:04 單次 operator interrupt 正常停止：

| 項目 | 結果 |
|---|---|
| Runtime / cycles | 最後 status uptime `1811.922s`；`357/357` successful、failed `0`、consecutive errors `0` |
| State / market | 全量 `181` 筆 status：`ACTIVE=51`、`PAUSED_MARKET=130`；raw spread 約 `1.9225–42.6674 bps`，超過 `30 bps` 後正確停止 targets/create；book/position 最大 age 約 `0.547s / 3.141s` |
| Position / mutation | position 全程 `0`；`would_place=202` 僅為模擬；真實 create/cancel/cancel-all、partial/full fills 全部 `0` |
| Fault counters | ambiguous submission/cancellation、reconciliation failure、unknown order、mutation-limiter block、HTTP 429、WS reconnect 全部 `0`；reconciliation success `357` |
| Resources | 22 個 worker checkpoint：RSS `132.95–134.43 MiB`、threads `15–26`、CPU `3.859–41.516s`；未見 busy loop 或無界成長 |
| Stop | console 精確記錄取消訂閱、Lighter disconnect 與 `Market maker stopped after operator interrupt`；Windows PTY exit `1` 仍是 ETX harness 狀態，不是應用原生 failure |

Independent account monitor 共保存 `112` 筆 sanitized risk snapshots，所有樣本均為 equity/free `299.882680`、BTC position `0`、account/BTC orders `0`。監控本身有兩次 `ClientConnectorDNSError`：第一個 monitor 因未包住該 read exception 而退出；fresh authenticated preflight 隨即通過後重啟，第二個 monitor 將另一次同類 fault 記錄並 bounded recovery，之後持續到策略停止。主 Market Maker 在兩次 observation 期間始終 failed cycle/429/WS reconnect/reconciliation failure `0`，book/position 狀態可信；兩次 fresh postflight 與 final risk snapshots 也完全一致。因此這是**已記錄的獨立監控 transport observation**，不是未解決的策略 safety fault，但下一次 live monitor 必須依第 12.6 節具備 bounded recovery 與 fail-closed escalation，不能再次無聲退出。

停止後兩次 fresh authenticated read 加一筆 raw account audit 均通過：Market Maker/monitor 程序 `0`、BTC/account orders `0`、BTC position `0`、`1x cross`、USDG total/free `299.882680 / 299.882680`、used `0`，BTC history count仍為 `33`，證明沒有 dry-run fill或 mutation。Config 保持 `dry_run: true` 且 SHA-256 未變。

結論：本輪 **cancellation-window repair offline + T3 software/safety gate GO（附 documented monitor DNS observation）**。這不覆蓋第 12.14 節 live NO-GO，也不等於新的 Step D live GO；該 checkpoint 當時停在 fresh Step D live 授權前。Emergency mutation budget 與新 cancellation window 的 live proof 都只能等待下一輪自然事件；當時 Step E、VPS 同步與 VPS 測試均未開始，後續 operator-directed Step E deviation 見第 12.16 節。

### 12.16 Operator-directed Step E：fee truth 與 E1 spread calibration（2026-08-23）

#### 授權偏差、目標與不變邊界

操作者明確指示在既有 commit/push 後直接跳到 Step E，目標是在可覆蓋實際手續費的硬條件下，以放大自然外部 maker 成交量為主要目標、淨盈利為次要目標。這是對「Step D 必須先 GO」的明確 operator-accepted deviation；第 12.12 與 12.14 節的 Step D NO-GO 歷史仍然成立，不得因進入 Step E 而改寫。

優先序固定為：safety／無自成交 > 實際 fee coverage > maker turnover／eligible quote hour > 淨盈利。只計自然外部成交，不得使用其他自有帳戶、策略或手動單製造 wash/self-trade；runtime 的 fill-event counter 不是成交 ledger，經濟結果以 authenticated unique BTC trades、每筆 fee tick、funding 與 flat-to-flat equity 為準。在 managed order 已明確簽署 integrator fee 為零，且 API `null` 已由本地 signer invariant 受限補足的前提下，精確 fee 為 `Trade Value × 該筆 maker/taker fee tick / 1,000,000`；CSV export 的 `Fee` 只有四位小數，只能交叉核對，不能作為小額高換手的精算來源。

既有 `POST_ONLY`、`max_raw_spread_bps=30`、`order_size=0.00020`、`max_position=0.00040`、exclusive/abort/pause/shutdown-cancel、BTC `1x cross` 與整輪 `5 USDG` hard ceiling 全部保留。每個 live stage 另採更嚴格的 `0.50 USDG` stage drawdown stop；任何 ambiguous submission/cancellation、failed cycle、reconciliation failure、unknown order、HTTP 429、非 maker fill、超過 position cap、帳戶狀態失去可信度或 repeated monitor read fault 都立即停止。這次指示不授權提高 position cap、leverage/loss ceiling，也不預先授權非 flat 時使用 IOC；若停止後非 flat，須停止切換參數並重新請示。

第 12.16 節原始逐參數 calibration 的每個 stage 最少觀察 15 分鐘、最多 30 分鐘；達到至少 `8` 筆 unique maker fills 且自然回到 flat 才可做完整經濟判定。`turnover = Σ abs(Trade Value)`；`gross = Σ account realized trade PnL`；`funding_cashflow` 使用帶正負號的帳戶 cashflow（credit 為正、debit 為負）；`net = gross - exact fee + funding_cashflow`。Flat-to-flat 且期間沒有 transfer 時，net 必須與 `equity_end - equity_start` 在顯示精度內一致，否則經濟結果為 INCONCLUSIVE。E1 是第一個可比較 live baseline，只要求全為 maker、`net >= 0`、gross 覆蓋 exact fee、保存 turnover／eligible hour 且所有 safety counters 通過；從 E2 起才要求 turnover／eligible hour 相較 E1 或上一個 accepted stage 上升。樣本不足只能記為 economic INCONCLUSIVE，不可宣稱已證明 cover fee。第 12.17 節的 long-run 候選明確取代此處「最多 30 分鐘」的 calibration 上限：前 15–30 分鐘只是同一 session 的 safety checkpoint，健康時不停止或重設 baseline。

#### E0：authenticated fee truth 單參數校準

Fresh read-only preflight 再次確認 Robinhood mainnet、premium tier、maker/taker fee tick `120/350`，對應已由 authenticated trade export 驗證的 `0.000120 / 0.000350`；BTC tick `0.1`、minimum base `0.00020`、minimum quote `10`。帳戶為 BTC position `0`、BTC/account open orders `0`、`1x cross`，USDG total/free `299.882680 / 299.882680`、used `0`；當時 BBO `77232.3 / 77248.0`，raw spread約 `2.0326 bps`。

E0 只把 `maker_fee_rate: 0.00050 → 0.000120`，其他參數不變；config SHA-256 為 `f667afe667411d09ee7fdeab2546b992fb73a9a29ce81403f580b34972c8a65c`。Config/strategy `26 tests / OK`；在 reference `77240.15` 下 configured half-spread仍為 `$50`，新的 fee-floor half約 `$11.2`，effective half仍為 `$50`。因此 E0 校準 fee truth，但不改變實際 quote geometry，不重跑一個無輸出差異的連線 gate。

#### E1：只縮 spread 與完整 T3 dry-run

E1 只把 `base_half_spread_ticks: 500 → 250`；maker fee truth 與其餘設定維持不變。Config SHA-256 為 `8e4563804d0e193e818ee33aba50d7679d188ab28fe0a6083bb43cab67753be4`，檔案保持 `dry_run: true`；loader 與 config/strategy `26 tests / OK`。在 reference `77240.15` 下 configured half為 `$25`、完整 quote約 `6.4733 bps`，仍高於雙邊 maker `2.4 bps + 0.5 bps` buffer 的動態 full-spread floor `2.9 bps`。

Exact-config T3 以 direct PTY 於 22:57:35 啟動，23:29:28 單次 operator interrupt 正常停止。完整 run 共 `190` 筆 status，最後 uptime `1904.359s`、`377/377` successful、failed/consecutive errors `0`；狀態為 `ACTIVE=26 / PAUSED_MARKET=164`。Raw spread 範圍 `3.9644–56.5768 bps`；所有 raw `<=30 bps` 的 ACTIVE 樣本均有雙邊 target，quote spread `6.47535–6.48865 bps`，所有 raw `>30 bps` 樣本均正確進入 PAUSED_MARKET 且 targets為空。Book/position 最大 age約 `0.453s / 3.094s`。

Position 全程 `0`，worst exposure `±0.00020`、utilization `0.5`；`would_place=104` 僅為模擬，真實 create/cancel/fill 全部 `0`。Ambiguous submission/cancellation、reconciliation failure、unknown order、mutation-limiter block、HTTP 429、WS reconnect全部 `0`，reconciliation success `377`。48 個 resource samples 為 RSS `133.047–134.391 MiB`、threads `15–25`、CPU `16.641→59.922s`，未見 busy loop 或無界成長。

停止後兩次 fresh authenticated read 均為程序 `0`、BTC/account orders `0`、BTC position `0`、`1x cross`、USDG total/free `299.882680 / 299.882680`、used `0`，BTC history count仍為 `33`，證明沒有 dry-run mutation/fill。E1 **software/safety T3 gate GO**；這不等於 live 經濟 GO。下一步必須先 commit/push 本節、確認 local/remote SHA、以同一 config 做 fresh flat preflight，且只有 raw spread回到 `<=30 bps` 才啟動受監控 E1 live。Step D 仍為歷史 NO-GO，Step E 下一個參數 stage 與 VPS 作業都尚未開始。

#### E1 首次 live：hard-stop propagation NO-GO，economic INCONCLUSIVE

E0/E1 文件 checkpoint 已先以 commit `816e03eae2f70500c7d08af821ac3d90349013e5` push 至 `feat/lighter-market-maker-mvp`，local/remote SHA 一致且 working tree clean。Live 前 fresh authenticated preflight 於 23:36:31 確認 Robinhood mainnet、premium maker/taker fee tick `120/350`、USDG total/free `299.882680 / 299.882680`、used `0`、BTC position `0`、BTC/account orders `0`、`1x cross`；BBO `77227.5 / 77242.9`，raw spread約 `1.99391 bps`。E1 dry config SHA-256 仍為 `8e4563804d0e193e818ee33aba50d7679d188ab28fe0a6083bb43cab67753be4`；只把執行副本切為 `dry_run: false` 後的 live SHA-256 為 `d4d436c5d0a9a040d46122a3bc3d024862a4269dda2c780fe72b410008773ed3`，沒有改動其他策略參數。

獨立 authenticated risk monitor 於 23:37:55 啟動，基準權益 `299.882680`、stage hard floor `299.382680`，每 `15s` 檢查權益、倉位、`1x cross` 與掛單；sanitized artifact 為 `logs/step_e_audit/risk-20260823T153753Z.jsonl`。Market Maker 以 direct PTY 於 23:39:27 啟動，明確記錄 `dry_run=False`；初始建立兩張 `BTC 0.00020 POST_ONLY` 雙邊報價。23:39:42 status 為 ACTIVE、`4/4` cycles successful、兩側 live remaining 均為 `0.00020`、quote spread約 `6.46862 bps`，ambiguous/reconciliation failure/unknown/429 均為 `0`。

23:39:59 的風控 snapshot 同時讀到 BTC active order `1`、account-wide active orders `0`。這個「account-wide 少於其 BTC 子集」的組合本身不可能證明存在非 BTC 掛單，但當時 monitor 將不相等錯誤分類為 `non_btc_order` 並依 hard-stop evaluator 自行退出。Account state 已失去可信度且 monitor 中斷本來就是立即停止條件；然而 monitor 只自行退出，沒有把 hard stop 傳遞給主程序，主程序到 23:41:12 才由 operator interrupt，中間約 `72.6s` 失去獨立監看。因此 hard-stop detection 有效，但 stop orchestration 本身亦為 NO-GO，不能因事後判斷為監看分類／跨 endpoint 一致性問題而忽略。

停止前 authenticated order inspection 只看到一張 `BTC BUY 0.00020 / limit / post-only / reduce-only`，沒有非 BTC 訂單證據；策略當時已到 `-0.00040 BTC` cap 並進入 `risk_reduction`。23:41:12 單次 operator interrupt 後完成 shutdown cancel、WebSocket cleanup 與 disconnect，23:41:19 寫出正常 operator-stop marker。最後 status uptime約 `102.984s`、`25/25` cycles successful、failed/consecutive errors `0`；create `6/6`、cancel `3/3`、full-fill events `2`、reconciliation success `25`，runtime ambiguous submission/cancellation、reconciliation failure、unknown、mutation limiter、HTTP 429 均為 `0`。Windows PTY exit `1` 仍是既知 ETX harness 表現，不是應用 traceback。

Authenticated unique trade ledger 在 `[23:39:27, 23:41:20]` 內只有兩筆，均為自然外部 maker sell：

| 時間（Asia/Taipei） | Side / role | Base | Price | Turnover | Exact fee |
|---|---|---:|---:|---:|---:|
| 23:39:50.946 | sell / maker | `0.00020` | `77315.3` | `15.463060` | `0.0018555672` |
| 23:39:58.521 | sell / maker | `0.00020` | `77321.7` | `15.464340` | `0.0018557208` |

合計 unique trades `2`、maker/taker `2/0`、turnover `30.927400 USDG`、exact fee `0.0037112880 USDG`、trade realized PnL `0`、funding `0`；ledger net realized為 `-0.0037112880 USDG`。兩筆都只是開倉腿，未達 `8` fills 且沒有 flat-to-flat close，因此不能用這個數字宣稱 fee coverage 或盈利能力。相對啟動 collateral 的顯示差額為 `-0.003712 USDG`，與費用扣款在六位顯示精度內相符，但仍不得替代非 flat 階段缺少的完整經濟驗證。

Config 已立即恢復 `dry_run: true`，SHA-256 回到 `8e4563804d0e193e818ee33aba50d7679d188ab28fe0a6083bb43cab67753be4`；Market Maker Python processes為 `0`。23:42:17 與 23:44:05 兩次 fresh authenticated postflight 均確認 BTC/account active orders `0`、BTC position `-0.00040`、`1x cross`、USDG total `299.878968`；兩次 free/used 分別為 `268.959328 / 30.919640` 與 `268.934928 / 30.944040`，差異來自未平倉期間的市場變動。

本輪判定為 **E1 live safety gate NO-GO / economic INCONCLUSIVE / STOPPED**。Hard-stop detection 有效，但 monitor → main stop propagation 未即時生效；operator interrupt 後的 shutdown cancel 有效。沒有 taker fill、cap breach、殘留掛單或 production grid code 變更；但 monitor 的 account-wide／symbol subset invariant 與 stop orchestration 都未能提供可信的持續稽核，且帳戶仍非 flat。依本節既定邊界，現在不得切到 E2、不得自行使用 IOC，也不得把兩筆 opening fills 算成已 cover fee。恢復前必須：

1. 由操作者處置 `-0.00040 BTC`，或另行明確授權最小必要的平倉方式；恢復前再做 fresh authenticated flat proof。
2. 重建 monitor 判定與 stop propagation：只有 `account-wide > BTC` 才可能表示存在其他 market orders；`account-wide < BTC` 必須視為跨 endpoint 不一致，bounded retry 後仍不一致就標記 `account_state_untrusted`。任何 hard stop 都必須主動 signal/request-stop 主程序，monitor 不得先自行退出，並須留存到確認 main process `0`；這條完成 deterministic proof 前不得重跑 E1。
3. 以相同 E1 config 重跑 live baseline並取得至少 `8` 筆 unique maker fills、自然 flat 與 flat-to-flat equity reconciliation；E1 經濟 GO 前不得開始 E2 spread stage。

### 12.17 Long-run 前最後優化與本機候選（2026-08-24）

#### 修復範圍與狀態

本輪只處理 Market Maker runtime、其測試與文件；沒有修改 Grid strategy／engine production code，沒有同步或測試 VPS，也沒有送出 live mutation。第 12.16 節 E1 首次 live 的歷史判定仍是 **safety NO-GO / economic INCONCLUSIVE**；本節完成的是下一輪候選的 offline 修復，不是新的 live GO。

外掛式 monitor 的 control-plane 缺口已改為主程序內建的 authenticated account audit worker：

- Coordinator 在第一張 quote 前先做 account-wide open orders、全部 positions、collateral 與 BTC authenticated trade baseline；候選設定要求從 flat、無任何 account open order 開始。Live 入口會拒絕 `account_audit_interval_seconds=0`。
- 每 `15s` 由同一 coordinator worker 稽核，單次讀取以 trades 前後 fence 加 position／balance 組成可信 snapshot；包含等待 cycle lock 的整輪上限為 `5s`，transport 或跨 endpoint mismatch 最多 bounded retry 三次。仍不可信、worker exception、taker／非標準／非本 runtime 成交、未知掛單、非 BTC position、超過 position cap或 drawdown 時，worker exception 直接觸發既有 `request_stop → shutdown cancel → unsubscribe → disconnect`，不再允許 monitor 靜默退出而主程序繼續。
- Account orders 只取一次 account-wide response並逐列檢查 symbol／ownership，不再比較兩個不同 endpoint 的 count，所以不會重現「account-wide 少於 BTC 子集」的錯誤分類。
- 已確認的 managed order id 會保留整個 runtime；即使 create acknowledgement 回來時訂單已完全成交，或 final exchange ID 稍後才由 history 解析，也能把 authenticated trade 精確歸因。合法 partial fill 後反向穿越零倉位會留在同一 flat-to-flat episode，不再誤停。Trade page 使用穩定 id 去重與數字 sequence 排序；一次 audit 若出現 `100` 筆全新 fills，因可能超出單頁窗口而 fail closed，不猜測缺失歷史。
- Authenticated account response 必須恰有一筆且 account identity 相符；collateral、nonzero position 的 unrealized PnL、IMF 與 margin mode 均採 finite／presence 嚴格解析，不能以缺值 fallback 成 `0` 或 `1x cross`。Shared Lighter adapter 仍接受交易所兩位小數表示的合法 IMF（例如 `33.33 → 3x`、`6.66 → 15x`）；只有 Market Maker audit 會另外以 raw IMF `100`、parsed leverage `1` 與 `cross` 三項同時鎖定本候選的精確 `1x cross`。
- Authenticated trade 的 maker/taker fee tick 與本 runtime 設定必須完全相符。每張 managed limit order 會在 signer 參數明確固定 integrator account/taker/maker fee 為 `0`；若 trades API 回報非零 integrator fee 必定 fail closed。主網歷史回報可能將零值表示為 `null`；新成交只有在訂單 ID 屬於本 runtime，且 adapter 仍提供上述零費率簽署保證時才可接受 `null`，否則仍 fail closed。
- Fail-closed／shutdown 的 cancellation、uncertain resolution 與 terminal-proof reads 使用 bounded safety-priority path，不受舊 read cooldown 卡住。Stop 的上限涵蓋等待 cycle lock 與 order-manager shutdown；即使該段 timeout，仍會繼續 unsubscribe、disconnect 並留下失敗終態，不會永久卡在鎖等待。
- Trade ledger 目前刻意只查 configured BTC market。測試 sub-account「沒有其他手動／自動 writer」是不可破壞的運行前提；account-wide orders／positions 仍會攔截持續存在的其他市場曝險，但不能偵測兩次 audit 間已完整開平的其他市場交易。

#### Fee cover 與成交量閘門

本候選保留兩個不同層級，不能互相替代：

1. **報價前 floor**：`maker_fee_rate=0.000120`、`min_profit_buffer_bps=0.5`，策略要求完整 quote spread 至少為 `2 × 1.2 + 0.5 = 2.9 bps`。若帳戶 authenticated fee tick 不再是 `120`，live 前必須先更新設定並重新驗證；live 中任一成交的實際 fee rate 不等於設定值會 hard stop。
2. **成交後 gate**：只使用 authenticated unique maker trades；`exact_fee = Σ turnover × actual fee tick / 1,000,000`，並以從 flat 出發、自然回到 flat 的完整 episode 累積 `gross`、`exact_fee`、`net_ex_funding` 與 two-leg `turnover = Σ abs(trade value)`。至少累積 `8` 筆 completed fills 後，已封口 episodes 的 `gross >= exact_fee` 與 `net_ex_funding / turnover >= 0.10 bps` 會立即評估，不會被同一 audit 尾端的新 opening inventory 遮蔽；任一失敗都自動 hard stop。兩者通過但目前 nonflat 時，狀態為 `fee_gate_pass_equity_pending_flat`，只有自然回到 flat 後才要求 `flat account-value change / turnover >= 0.10 bps` 並給出完整 runtime GO。

`0.10 bps = 0.001%` 是扣除精確交易 fee 後、相對雙腿成交量的 session 累積門檻。它刻意不是「每一筆 fill 的盈利百分比」：開倉腿尚未有完整 realized PnL，逐筆判斷會產生假 NO-GO；累積 flat-to-flat 也比任選買賣配對更可重建。`8` completed fills 是最早 fail-fast 門檻，不是耐久盈利證明；未來從 E1 promotion 到 `200 ticks` 前，人工證據另要求至少 `10` 個 completed round trips及 funding／transfer postflight 核對。`min_profit_buffer_bps=0.5` 使用單程 reference notional，而 post-trade 分母包含兩腿 turnover，因此兩個 bps 數字不可直接當作同一分母比較。

Status 的 `account_audit` 另提供 `unique_maker_fills`、`turnover`、`maker_fills_per_wall_hour`、`maker_turnover_per_wall_hour`、`maker_fills_per_eligible_hour`、`maker_turnover_per_eligible_hour`、completed round trips、exact fee、gross、fee-cover ratio、net/turnover bps、drawdown、flat equity change與 `unattributed_flat_cashflow`。目前 runtime 未另查 authenticated funding／transfer cashflow；因此 `gross >= exact_fee` 與 `net_ex_funding` 不會把正 funding credit 拿來掩蓋未 cover fee，但 `fee_and_equity_gate_go` 仍只是 runtime gate。正式 session economic GO 必須在停止後以 authenticated funding／transfer 與 flat equity 差額交叉核對。

#### 最後候選與操作者動作

本機 `config/market_maker/test_lighter_btc_mvp.yaml` 的最終 dry-run SHA-256 以本節驗證結果為準。策略幾何保留 E1 `both / order 0.00020 / max position 0.00040 / configured half-spread 250 ticks / reprice 250 ticks / max raw spread 30 bps / 30s lifetime / 8 mutations per minute`；effective half-spread仍會取 base 與 fee floor 的較大值再向 tick ceil。本輪沒有直接縮到 `200 ticks`，因上一輪失敗點是 monitor control plane，且尚未建立可信 E1 live turnover baseline。未來要提高成交量時，應先讓此候選取得 safety/economic GO，再只改 `250 → 200`，以 `maker_turnover_per_eligible_hour` 做同口徑比較。

設定檔安全預設仍為 `dry_run: true`。操作者開始長測前：

1. Fresh authenticated read-only preflight 必須確認正確 Robinhood mainnet 測試 subaccount／wallet owner、BTC/account open orders `0`、BTC position `0`、`1x cross`、maker fee tick `120`，且沒有其他手動或自動策略。舊文件的 position 只是歷史快照，不可取代 fresh read。
2. 本輪 monitor／adapter／stop-propagation 修復依第 5.1 節完成一次完整 `30m` T3（且至少 10 次 `account_audit`）；結果記在下節。此 fingerprint GO 後，live 前可另做約 2–3 分鐘 startup canary，確認 audit state `healthy`、failed cycle `0`、真實 mutation `0`，不必再重複一輪 30 分鐘。後續修改仍依第 5.1 節分級，只有真正 T0–T2 變更才能使用縮短 gate。
3. Live 執行副本只把 `dry_run` 明確改成 `false`；wallet profile 只透過 `--wallet-name <profile>` 傳入，不把 key/account 寫入 YAML。前 `15–30m` 視為同一程序的 safety canary：audit 必須持續 healthy，且 failed cycle、uncertainty、taker、unknown 與 429 均為 `0`。若因成交樣本不足仍為 `economic_state=collecting`，可讓同一 session 繼續 long-run；累積至少 `8` completed fills 時立即判定 fee/net，失敗即 hard stop；通過但 nonflat 時只等待自然 flat 完成 equity gate。Cleanup 只在最終停止後驗證，不能作為「同一程序繼續」前置條件。
4. 候選固定採第 12.16 節較嚴格的 `max_session_drawdown=0.50 USDG`，canary 與長測不得中途重設 baseline。先前授權的 `5 USDG` 只是整輪外層上限，不會覆蓋這個較嚴格的自動 hard stop；本輪不建議放寬，也不得同時提高 order size、position cap或 leverage。
5. 出現 `account_audit.state=hard_stop`、程序退出、任何 uncertainty／taker／unknown、position cap、drawdown或經濟 NO-GO 時，不得自動重啟。Shutdown 只撤 managed orders，不自動 flatten；須 fresh authenticated 確認 orders／position，若非 flat 再由操作者決定處置。
6. Runner 沒有 `15–30m` 或 `2h` 自動 duration。前 `15–30m` 只是 live safety checkpoint：健康時不要 Ctrl+C，讓同一 session 與 drawdown baseline直接繼續；只有異常停止條件或最終 `2h`／人工 long-run 界線才由操作者 Ctrl+C。不得使用自動重啟器。

建議命令（先 dry-run）：

```powershell
.\.venv\Scripts\python.exe run_market_maker.py config\market_maker\test_lighter_btc_mvp.yaml --dry-run --debug
```

目前本機測試 sub-account 由預設 Lighter config 載入，所以命令不加 `--wallet-name`。只有完整且已驗證的 named profile 才另加 `--wallet-name <profile>`。Live 時沿用同一命令，但使用已明確切為 `dry_run: false` 的執行副本並移除 `--dry-run`；程式本身不提供任何 CLI 參數把安全預設反向切成 live。

### 12.18 Long-run 最後候選 T3 與交接（2026-08-24）

真實 trades API 會將歷史成交的零 integrator fee 回報為 `null`，因此曾有一次 startup fail-closed；修復後的第一次短輪又因獨立審查發現 `False == 0` 的 capability 型別邊界而主動中止。這些輪次均不計入 T3。最終修復明確在 signer 中固定 integrator account/taker/maker fee 為 `0`，trade `null` 只在「嚴格整數零 capability + 本 runtime managed order ID」同時成立時接受；非零、無證明或非本 runtime 成交仍 hard stop。

最終候選 YAML SHA-256 為 `B8FD57C29BBD7A51A54B6769647F9F74A8CFA62BECD97F8BE993902FF7419475`，檔案與 CLI 同時保持 dry-run。Fresh authenticated preflight 確認 Market Maker 程序 `0`、account/BTC open orders `0`、BTC position `0`、餘額未佔用與 wallet ownership PASS。正式 T3 於 `02:01:54–02:33:13` （Asia/Taipei）執行；最後 status uptime `1872.344s`、account session age `1870.547s`、cycles `360/360` successful，failed/consecutive errors `0`。

前段在 raw spread 合格時累積 `would_place=226`、eligible quote seconds `586.532`，quote edge after fees 約 `4.07–4.09 bps`；後續 raw spread 超過 `30 bps` 後正確切到 `paused_market`、targets 為空且 eligible time 不再增加。全程 account audit `healthy`、total read failures `0`；WS reconnect、ambiguous submission/cancellation、reconciliation failure、unknown orders、mutation limiter block、HTTP 429、真實 create/cancel/fill 全部 `0`。Position、equity 與 drawdown 全程不變；因 dry-run 無成交，economic state 保持 `collecting`，本輪只驗證 software/safety，不宣稱已實證 cover fee。

資源樣本 RSS 約 `132.793 → 133.555 → 134.574 → 137.781 → 138.363 → 138.969 → 138.520 MiB`，threads `16 → 18 → 24 → 28 → 32 → 32 → 34`。30 分鐘後額外 25 秒六點樣本為 RSS `138.51–138.53 MiB`、threads `34 → 33`、handles `595 → 593`，顯示 thread-pool 擴張後趨穩，未觀察到 busy loop 或無界資源成長。單次 operator interrupt 完成 unsubscribe/disconnect；Windows PTY exit `1` 是既知 ETX harness 表現，無 traceback。Fresh postflight 確認程序 `0`、open orders `0`、position `0`、餘額未佔用，config SHA 未變。因此本 fingerprint 判定 **offline regression + T3 software/safety gate GO**。

回歸證據為關聯 `79 tests / OK`、全部 Market Maker `216 tests / OK`、`compileall` 與 `git diff --check` PASS。全專案 `451 tests` 維持與修復前相同的既知 Grid selective-cancel 基準 `8 failures + 4 errors`，本輪沒有新增失敗類型，也沒有修改 Grid production code。

操作者現在可用第 12.17 節候選進行手動 long-run：只在執行副本把 `dry_run` 改為 `false`、移除 CLI `--dry-run`，其餘參數不變。啟動前仍須 fresh preflight；前 `15–30m` 只是同一 session 的 canary，健康時不停止或重設 drawdown baseline。達 `8` completed fills 後 fee/net 立即判定，通過但 nonflat 時等待 equity gate；若 hard stop 或程序退出，不得自動重啟。本次 T3 GO 不改寫 Step D 歷史 NO-GO，也不會在取得 E1 safety/economic GO 與至少 `10` 個 completed round trips 前允許縮至 `200 ticks`。
