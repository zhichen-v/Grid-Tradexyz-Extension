# Market Maker MVP 操作指南

本指南適用於單一 Lighter 永續合約交易對的 Market Maker MVP。所有命令均從 repository 根目錄執行。

> **硬性安全規則**
>
> - Dry-run 依第 5.1 節的風險分級執行；首次新 fingerprint 或重大事故採完整 T3。Phase 8 live rollout **只能由操作者明確執行**；automated test 不得連線送出真實交易 mutation。
> - 一般報價必須為 `POST_ONLY`；嚴禁 self-trade、跨自有帳戶互相成交或任何形式的洗量。
> - 程式停止時只撤單，**不會自動 flatten position**。停止後的持倉由操作者另行評估與處理。
> - 每次停止（含緊急停止）最後都必須透過 authenticated REST 或 Lighter 交易所介面確認目標 symbol 的 `open orders = 0`。未歸零、無法查詢或取消結果不確定，都屬事故，不能視為正常停止，也不得重新啟動。

## 1. 環境安裝

需求：Python 3.12、`uv`、repository 既有依賴。PowerShell：

```powershell
Set-Location '<repository-root>'
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

## 5. 分級 Dry-run Gate

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

第一次建立新 runtime／account／config fingerprint 時執行完整 30 分鐘 T3；之後依第 5.1 節按風險縮短。每輪都留下下列紀錄：

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

任一 **hard safety gate** 失敗即為 **NO-GO**：立即停止、保留 log，修正後依第 5.1 節選擇相稱的 revalidation。未通過不得進入 live rollout。Economic gate 的門檻不得降低或略過；但 economic NO-GO 只淘汰該 parameter candidate，不會把已建立的基礎 runtime safety fingerprint 一併作廢，也不會自動要求每個 MM-only 定價修正重跑 30 分鐘。

### 5.1 風險分級 revalidation（2026-08-23 起適用）

完整 30 分鐘是新 fingerprint 或重大事故後的最高強度 gate，不是每次調參的固定成本。已有綠色 T3 baseline 時，依實際行為影響選 tier；可把同一候選的相關小修合併後只驗一次。無法證明低風險時向上選擇。

| Tier | 最短連線 dry-run | 適用範圍 | 額外通過條件 |
|---|---:|---|---|
| T0 | 0 分鐘 | 純文件、註解、測試；或 loader 結果完全相同的格式調整。 | 受影響的離線測試；runtime/config fingerprint 不變。 |
| T1 | 5 分鐘 | 純 log、metrics、redaction，且不改變 timing、錯誤處理、quote、risk 或 order state。 | 至少 50 個 successful cycles、failed `0`；受影響的週期性讀取至少 3 筆。 |
| T2 | 5–10 分鐘 | **MM-only 局部策略／定價變更**，沒有改 shared adapter、position/exposure、mutation/reconciliation、data freshness 或 control-plane。 | 先跑 deterministic unit tests 與全部 `test_market_maker_*.py`；exact-config dry smoke 至少 50 cycles（建議 100），hard-safety counters 全為 0。 |
| T3 | 15–30 分鐘 | Runtime/order lifecycle、shared adapter、parser/data、position/risk、create/cancel/reprice、uncertainty/reconciliation、rate limit、account audit/control-plane、shutdown、SDK/profile/network，或 hard-safety 事故修復。 | deterministic 事故測試 + MM suite + 相關 shared/Grid safety regression；新 fingerprint、uncertainty 或 control-plane 事故取 30 分鐘，其餘可取 15 分鐘。 |

所有會執行的 Market Maker 程式改動都先跑 targeted tests 與 `unittest discover -s tests -p "test_market_maker_*.py"`。只有 T3/shared runtime 變更再跑相關 Lighter/Grid safety slice與完整 `test_*.py`，並只比對既知 baseline，不修理不相關 Grid 問題。T1–T3 前保存 code/config fingerprint、SDK/network/account、獨占、程序／orders／position、metadata/fee/margin；dry-run 同時保留 YAML `dry_run: true` 與 CLI `--dry-run`。停止後確認程序與 orders 為 `0`、position/fills 未變、真實 mutation 為 `0`。

Gate 綁定最後候選的 code/config/account fingerprint。Hard safety NO-GO 依變更面回到 T2 或 T3；economic NO-GO 則保留 runtime safety證據，只淘汰該參數候選，修正後通常走 T2。不得在 Robinhood 主網刻意製造斷網、不確定送單或自成交；事故路徑用 deterministic fake/Mock。**每一輪 live 都仍需新的操作者明確授權**；縮短 dry-run 不等於沿用 live 授權。

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
3. 檢查 exit code 與 critical log。一般應為 `0`；Windows direct PTY 可能把已處理的 `Ctrl+C` 回報為 `1`，此時只有同時看到 operator-interrupt、unsubscribe／disconnect、程序消失及兩次 authenticated `open orders = 0` 才可判定 clean stop。其他非零、cancel failure或uncertain cancellation均屬異常。
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
8. 找到根因並通過相稱的 revalidation 前不得重啟 live；uncertainty、shared runtime 或 control-plane 事故使用 T3 30 分鐘。

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

## 12. Phase 8 本地驗證與交接（2026-08-26）

> **目前狀態：OPERATION PAUSED / ACCOUNT FLAT。** 2026-08-26 08:49（Asia/Taipei）策略與 recovery 程序都已停止；兩次 fresh authenticated postflight 均為 process／position／open orders／unresolved `0/0/0/0`，collateral `299.555839 USDG`。目前沒有任何 Market Maker live 程序或掛單。

### 12.1 現行候選與硬邊界

| 項目 | 現行值 |
|---|---|
| 帳戶／環境 | 隔離 Lighter sub-account、Robinhood mainnet、BTC 獨占；每次 live 重新驗證 |
| Source config | `config/market_maker/test_lighter_btc_mvp.yaml`；`dry_run: true`；SHA-256 `739E64B38DE9106204082283CC1FD52B21C58B56B32307E0523F8178E975901F` |
| MM code fingerprint | SHA-256 `EFA8376E1C0EBEB9F32D89141DF07BD2A52A7DF2F53C2EDC1BDF8B953B674ED5`；依路徑排序，對 `run_market_maker.py` 與 `core/services/market_maker/*.py` 逐檔串接 `path + NUL + bytes + NUL` |
| Live copy | 受 ignore 保護；相對 source 的 semantic diff 只能是 `dry_run: false` |
| Quote／risk | `both / half-spread 250 ticks / order 0.00020 / max position 0.00040 / 1x cross / max drawdown 0.50 USDG` |
| Fee／exit | maker `1.2 bps`；timed soft exit `120s`；surplus reserve `0.02 bps`；所有正常與 recovery quote 維持 `POST_ONLY` |
| Runtime | raw-spread guard `30 bps`；audit `15s`／timeout `10s`；讀取 `5 attempts × 1s`；mutation `8/min` |
| Economic gate | 同一 session 至少 `30 completed maker fills`；fee cover `>= 1`；completed net／turnover `>= +0.02 bps`；自然 flat 後核對 equity／funding |

優先序固定為：hard safety／無自成交 > 實際 fee coverage > maker turnover／eligible hour > 淨盈利。不得用 taker／IOC、提高 size／position／leverage／drawdown、跨自有帳戶成交或隱藏既有損失來加速。

### 12.2 最新決定性證據

| 候選／事件 | 決定性證據 | 判定 |
|---|---|---|
| `250 ticks / 8 mutations` + 降低庫存側優先 | 約 `1759s`；38 completed maker fills／15 round trips；turnover `690.898688`；exact fee `0.08290784256`；net `+0.00206015744`；`+0.0298185 bps`；cover `1.0248488`；flat-equity `+0.0298162 bps`；約 `1416 USDG/h`、`77.9 fills/h`；max DD `0.031468` | **前一 live fingerprint 的短場次 safety／economic GO**；live 已證明 mutation 緊張時先補降低庫存側；1 次瞬時 account read 由 bounded retry 吸收 |
| `250 ticks / 10 mutations` 單參數 canary | 約 `650s`；停止前 22 completed／10 round trips；completed turnover `345.085000`、net `+0.00126980000`、`+0.0367967 bps`、cover `1.0306639`；runtime wall-rate（含第23筆open authenticated fill）約 `2005 USDG/h`、`127.8 fills/h`；max DD `0.015979` | **INCONCLUSIVE**：操作者在30筆前要求停止；有1次 cancellation terminal-visibility race，精確同步後解除，無duplicate／unknown／reconciliation failure |
| 10/min 停止與人工授權 cleanup | 停止窗口留下 short `0.00020`；只用 `POST_ONLY + reduce_only BUY 0.00020 @ 78517.7` maker 回補。Authenticated trade-ledger全場 turnover `376.491440`、net `-0.00313897280`、`-0.0833743 bps`、cover `0.9305214`；baseline／final equity `299.558978 / 299.555839`，與ledger只差 `2.72e-8 USDG` | **本場完整 economics NO-GO**；不得把 completed-episode 正值誤稱為全場 fee-cover GO。10/min吞吐比較仍屬INCONCLUSIVE，已恢復 `8/min`且尚未promotion |
| 終止證明 | 最新20筆交易全部 authenticated maker／tick `120`；兩次 postflight 皆為 position／orders／unresolved `0/0/0`；process `0` | **Clean handoff** |
| Offline regression | Market Maker `278/278 OK`；full repo `515` 維持既知 Grid／selective-cancel baseline `8 failures + 4 errors`，沒有新增失敗 | **MM GO；不修理本輪範圍外的 Grid baseline** |

本輪已完成：account snapshot `5 × 1s` bounded retry、WS processed fill後才信任新soft-exit audit、startup retry重建baseline、mutation budget緊張時優先建立降低目前庫存的一側、受控停止、maker reduce-only平倉及雙postflight。未修改Grid production code或shared Lighter adapter，也未執行VPS同步。

### 12.3 Promotion gate

只有同一 250-tick fingerprint 同時滿足下列條件才可進 long-run：

1. Fresh preflight 為 flat、open orders `0`、unresolved `0`、BTC 獨占、`1x cross`，fee tier 與風險值正確。
2. 同一 session 達 `30 completed maker fills`，且自然回到 flat。
3. Taker fill、unknown order、**終止時未解決**的 uncertain mutation、reconciliation failure、account-untrusted hard stop、position cap breach與 `0.50 USDG` drawdown breach全部為 `0`；已精確解決的 terminal-visibility race仍需逐筆留證，不得隱藏或重送 signer mutation。
4. Exact fee cover `>=1`、completed net／turnover `>=+0.02 bps`，flat equity差額已對帳。
5. 正常 shutdown 後程序消失，兩次 authenticated postflight 均為 position／orders／unresolved `0/0/0`。

參數候選維持 `8/min`；前一live fingerprint已通過短場次gate。目前checkpoint撤回未完成的fill-generation實驗後未再live，且P0／P1尚待完成，因此不得直接進多小時long-run。`10/min`只顯示較高早期成交率，因樣本不足、發生一次已解決的cancellation race，且操作者停止後的全場結果未cover fee，所以已回退，不能寫入正式long-run config。

未完成工作依序為：

1. P0：將OrderManager的 `fill_observed` 與純cancel也會觸發的 `position_refresh_required` 拆開，補「同淨倉位雙fill」與「純reprice不失效」測試；未完成前不進long-run。
2. P1：讓RiskManager與Strategy共用單一authoritative inventory age／soft-exit latch，並補data／error recovery timer drift測試；目前雙timer只會保守延後soft exit，未放寬exposure或fee-cover邊界，但會影響換手。
3. 完成P0／P1、targeted與MM／full regression後，重算code fingerprint並做相稱T3；再以該最終fingerprint／`8/min`從fresh flat preflight重跑至少30 completed且自然flat的短gate。
4. 短gate通過後再執行多小時long-run，保留自然flat checkpoints、資源與authenticated fee/equity證據。
5. 若仍以放大成交量為主要目標，可另開全新 `10/min` 單變因場次，至少完成30筆並自然flat；與8/min比較quote uptime、mutation blocks／hour、terminal race密度、turnover／hour與全場fee cover後再決定是否promotion。
6. 若250 ticks仍在單向行情長時間鎖倉，先研究MM-only momentum／toxicity entry guard；不得同輪縮spread或放大size／position／leverage／drawdown。
7. VPS同步與測試仍不在本階段範圍。

歷次事故、逐輪數字與舊 fingerprint 已移至 [Market Maker MVP 驗證歷史](market_maker_mvp_validation_history.md)；它們不是 fresh preflight 或目前 GO 的替代品。
