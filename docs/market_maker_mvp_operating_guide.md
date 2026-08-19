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
| `base_half_spread_ticks` | 單側距離，不是完整 bid-ask spread；至少 1 tick。 |
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

- 目前 MVP config **沒有單邊報價開關**，因此現版程式不得直接執行此步。若要進行 Step B，必須先新增單邊模式、完成同等單元／整合測試並重新通過 dry-run gate；不可用錯誤 config、手改 runtime 或在 live 帳戶刻意製造危險持倉來模擬。
- 驗證所有一般限價單為 `POST_ONLY`，並逐一確認 create、cancel 與 shutdown 的交易所回報。
- 未確認 cancel terminal 前不得 replacement。

### Step C：雙邊極小額

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
