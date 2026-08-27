# Market Maker MVP 操作指南

本指南是單一 Lighter 永續合約 Market Maker MVP 的操作與安全來源。所有命令由 repository 根目錄執行；歷史逐輪證據另見 [驗證歷史](market_maker_mvp_validation_history.md)。

## 1. 不可放寬的規則

- 一般與 recovery quote 一律 `POST_ONLY`；禁止 taker／IOC、self-trade、跨帳戶互成交或任何形式的洗量。
- Live 每一輪都需操作者明確授權；縮短 dry-run 不等於沿用 live 授權。
- 目標 account／symbol 必須獨占，且啟動前為 flat、open orders `0`、無其他 bot 或手動策略。
- Stale／untrusted data、unknown order、uncertain mutation、reconciliation failure、account audit hard stop、position／drawdown breach 都須 fail closed。
- 停止先撤單，不自動平倉。若另有明確授權，只能用有界 `POST_ONLY + reduce_only` 處理殘倉；不得用 taker 加速。
- 每次停止後必須做兩次 authenticated REST 檢查，確認程序、目標 symbol open orders 與 position 都為 `0`。查不到或狀態不確定即為事故。
- 不得輸出或提交 private key、token、wallet profile、signer、`.env*`、live config 或 log。

安全優先序固定為：hard safety／無自成交 > 實際 fee cover > maker turnover > 淨盈利。

## 2. 安裝、credentials 與隔離

需求為 Python 3.12、`uv` 與 repository 既有依賴；已有 `.venv` 時不要重建。

```powershell
Set-Location '<repository-root>'
uv venv --python 3.12
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

YAML 不保存 credentials。未指定 profile 時沿用既有 Lighter bootstrap；指定 profile 時只讀 `.env.wallets/<profile>.env`，CLI 使用 `--wallet-name <profile>` 或既有 shortcut。不得把 credential 值放在命令列或 log。

Live 前以 authenticated read-only preflight 核對：

1. Network、account index、L1 owner、symbol 與 fee tier正確。
2. 專用 sub-account／symbol 無其他策略、手動掛單或可能互成交的自有帳戶。
3. Position `0`、open orders `0`、未解決 submission `0`。
4. Margin 為預定 `1x cross`，可用資金與 drawdown 邊界正確。
5. Exchange tick、step、minimum 與 order notional 合法；不合法時中止，不能自動放大 live order。

## 3. Config 核對

範例為 `config/market_maker/lighter_btc_mvp.example.yaml`。金融值使用 YAML 字串與 `Decimal`。

| 類別 | 必查欄位與意義 |
|---|---|
| 報價 | `quote_mode`、`order_size`、`base_half_spread_ticks`、`reprice_threshold_ticks`、`min_order_lifetime_ms`。`both` 才是一般雙向策略；模式切換前先停機並確認 orders `0`。 |
| 市場資料 | `max_raw_spread_bps`、`stale_book_seconds`、`trend_guard_window_seconds`、`trend_guard_threshold_ticks`；外部 BBO 過寬／過期即停止 create，趨勢 guard 只擋逆勢風險增加側。 |
| 庫存 | `max_position`、`max_inventory_skew_ticks`、`soft_position_ratio`、`hard_position_ratio`；hard zone 只能保留 reduce-only 側。 |
| 同步 | `position_poll_interval_seconds`、`order_sync_interval_seconds`、`health_check_interval_seconds`；unknown position 不得當成 `0`。 |
| 帳戶審計 | `account_audit_interval_seconds`、`account_audit_timeout_seconds`、`require_flat_start`、`max_session_drawdown`；live 不可關閉 audit。 |
| 經濟門檻 | `maker_fee_rate`、`min_profit_buffer_bps`、`economic_min_fills`、`min_completed_net_turnover_bps`、`max_session_loss_for_maker_exit`；completed ledger 與自然 flat equity 都需過門檻，loss budget不是GO豁免。 |
| 錯誤／流量 | `max_consecutive_errors`、`error_cooldown_seconds`、`max_mutations_per_minute`；安全撤單優先，禁止 busy retry。 |
| Ownership | `exclusive_symbol_control: true`、`startup_open_order_policy: abort`、`unknown_order_policy: pause`。未知單不可猜測為本策略所有。 |
| 必要開關 | `post_only: true`、`cancel_on_shutdown: true`；source config 維持 `dry_run: true`。 |

Live copy 必須受 Git ignore 保護，且相對 source 的 semantic diff 只能是 `dry_run: false`。

## 4. 測試與分級 dry-run

任何 MM 程式變更先跑受影響測試及：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_market_maker_*.py"
```

Runtime／shared 變更再跑完整 `test_*.py`，只比對既知 baseline，不修理不相關 Grid 問題。Dry-run 同時保留 YAML `dry_run: true` 與 CLI `--dry-run`：

```powershell
.\.venv\Scripts\python.exe run_market_maker.py <config> --dry-run --debug
```

| Tier | 時長 | 適用範圍與最低證據 |
|---|---:|---|
| T0 | 0 分鐘 | 文件、註解、測試或 loader 等價調整；跑受影響離線測試。 |
| T1 | 5 分鐘 | 只改 log／metrics／redaction，不改 timing、quote、risk 或 order state；至少 50 successful cycles、failed `0`。 |
| T2 | 5–10 分鐘 | MM-only 定價／策略局部調整，未改 exposure、mutation、reconciliation 或 data trust；deterministic tests、MM suite、至少 50 exact-config cycles。 |
| T3 | 15–30 分鐘 | Order lifecycle、position/risk、uncertainty/reconciliation、control-plane、shutdown、shared adapter 或 hard-safety 修復；新 fingerprint 或重大事故取 30 分鐘。 |

每輪保存 code/config fingerprint、account／network／metadata、CPU/RSS、cycles、book/position age、quotes、reconnect、429、mutation、uncertainty、reconciliation 與真實 mutation 數。Dry-run 的 create/cancel 必須為 `0`；任何 hard-safety counter 非零即 NO-GO。

## 5. Live gate 與 long-run promotion

Live 只在 fresh preflight 與相稱 dry-run 通過後啟動：

```powershell
.\.venv\Scripts\python.exe run_market_maker.py <ignored-live-config> --debug --wallet-name <profile>
```

短 gate 最長 60 分鐘，必須同時滿足：

1. 至少 `30 completed maker fills`，並自然回到 flat。
2. Taker、unknown order、未解決 uncertain mutation、reconciliation failure、account hard stop、429／reconnect storm、position／drawdown breach皆為 `0`。
   Cancellation 判定看目前狀態：`unresolved_cancellations=0`，且累計 `ambiguous_cancellations` 必須被同等數量的 `resolved_ambiguous_cancellations` 以 exact terminal proof 對消；未對消差額立即 hard stop。歷史已解決事件仍保留在 log，不得直接把累計 ambiguity 清零。
3. Exact fee cover `>= 1`，completed net／turnover與 flat equity／turnover都 `>= +0.02 bps`。
4. Graceful shutdown 後程序消失，兩次 authenticated postflight 都為 position／orders／unresolved `0/0/0`。

`max_session_loss_for_maker_exit`只允許已鎖定的`POST_ONLY reduce-only`退出使用，且必須採 exact trade P&L 與同 generation 的最後flat-equity證據較差者；證據缺失／過期時退回嚴格fee-aware價格，超限即hard stop。`bounded_economic_recovery`明確不是GO。

任一 hard-safety 失敗立即停止並保留證據；不得為湊成交數而忽略。Economic NO-GO 只淘汰該參數候選，門檻不得降低。只有短 gate 完整 GO 才可從新的 fresh-flat session 啟動數小時 long-run。

Long-run 每 30 分鐘保存：completed fills／round trips、turnover、exact fee、net／bps、fee cover、flat-equity reconciliation、fills/h、USDG/h、max drawdown／position、create/cancel／blocks、uncertainty／unknown／reconciliation、429／WS、CPU/RSS。自然 flat checkpoint 才能形成完整經濟證據。

## 6. 停止與事故處置

正常停止：

1. 在直接 PTY 按一次 `Ctrl+C`，不可用輸出 pipeline 代管 runtime。
2. 等待 `STOPPING`、managed-order cancel、REST reconciliation、final audit、unsubscribe 與 disconnect。
3. Windows PTY 可能回報 exit `1`；只有同時看到 operator interrupt、正常 cleanup、程序消失與雙 postflight 全零才可視為 clean。
4. 若 position 非零，先保持停機；只有另行明確授權才做有界 maker reduce-only recovery。

| 現象 | 必要處置 |
|---|---|
| Stale book／position、WS unhealthy | 停止 create，撤除可確認單，REST sync；反覆發生即停機。 |
| Submission uncertain | 視為訂單可能已存在，禁止 duplicate／換 client id 重送；查 open orders/history。 |
| MM create 的 DNS pre-connect | 只有 configured API host 的精確 `ClientConnectorDNSError`，且 optimistic nonce 已成功回滾時，才可記為「未送出」並於下一cycle重試；其他timeout／斷線／錯誤host仍走uncertain。此例外只由MM opt-in，Grid預設不變。 |
| Cancellation uncertain | 視為仍 live並計入worst exposure；OPEN／PARTIAL不得清 uncertainty，未取得同ID exact terminal前禁止replacement或重送cancel。`FILLED/CANCELED/REJECTED/EXPIRED`皆可證明該order outcome已終結；adapter與manager marker須同時歸零。Cancel競速若同周期取得exact `FILLED`，須記cancel `success=false`、refresh position且不得立即replacement；因未曾公開為ambiguous，不增加resolved counter。 |
| Unknown order | Pause；以 authenticated REST 辨識。非 exclusive symbol 不可 cancel-all。 |
| HTTP 429 | 讓 shared cooldown 生效；禁止快速重啟或無限 retry。 |
| Position cap／drawdown | 只保留 reduce-only；達 hard limit 立即停機。 |
| Shutdown cancel／final audit failure | Critical incident；程序結束不等於訂單已撤，執行下列人工程序。 |

事故程序：停止 watchdog／新 mutation，終止殘留程序，authenticated 查詢同 account／symbol，取消可證明屬於本策略的殘單，重查至 open orders `0`，核對 position/fills，保存 log。任何狀態仍不確定時維持事故狀態，不得重啟；uncertainty／reconciliation／shutdown 事故修復後使用 T3 30 分鐘。

## 7. Phase 8 rollout

| Step | 內容 | 晉級條件 |
|---|---|---|
| A | Read-only dry-run | Metadata、WS/REST、position／orders、完整 gate；真實 mutation `0`。 |
| B | 單邊最小 live | `bid_only` 或 `ask_only`；逐一證明 POST_ONLY create、cancel、terminal 與 shutdown。 |
| C | 雙邊最小 live | `both`、最小 size、保守 spread／mutation；30–60 分鐘監看 exposure、fills 與 pause。 |
| D | 小額長時間 | 只在短 gate GO 後執行數小時；觀察 drift、latency、fee、funding、資源與事故密度。 |
| E | 單變因調參 | 一次只改 spread、size、skew、ratio 或 refresh 其中一項；每次重新驗證。 |

## 8. 本地 checkpoint（2026-08-28 00:10 Asia/Taipei）

### 8.1 候選與邊界

| 項目 | 現行值 |
|---|---|
| 帳戶／市場 | 隔離 Lighter sub-account、Robinhood mainnet、BTC 獨占 |
| Source config | `config/market_maker/test_lighter_btc_mvp.yaml`，`dry_run: true`，SHA-256 `27266EF375984F8DA3A1724FC9F4EB50B625CDDEB746052E37C2B9057EEC5463` |
| Current code fingerprint | SHA-256 `200159C7F983A09113CF8B83148E76E398B7508367BBD104BCFA227E7701ABCE`；入口＋MM核心＋shared Lighter adapter／exception，共13個runtime Python檔 |
| Quote／risk | `both / 250 ticks / 0.00020 / max position 0.00040 / trend 60s/125 ticks / 1x cross / drawdown 0.50 USDG / 8 mutations/min` |
| Fee／exit | maker `1.2 bps`、soft exit `120s`、maker-exit session loss最多`0.10 USDG`、全部 `POST_ONLY` |
| Economic gate | `30 completed`、fee cover `>=1`、completed與flat-equity皆 `>=+0.02 bps` |

### 8.2 本輪結果

| 階段 | 證據 | 判定 |
|---|---|---|
| 前一短live基線 | 60分鐘只有5 maker fills；第三個short episode未自然flat | Runtime／safety GO，volume／economic NO-GO；細節見history `E2s` |
| 趨勢＋loss-budget修復 | 方向感知hysteresis、reduce-only豁免、long／short exact tick邊界、累積trade／flat-equity budget、證據generation與coalesced close+reopen均有deterministic tests；受影響`185/185`、MM`315/315`，full repo`559`仍為既知Grid `8F+4E` | Offline GO；Grid production未修改，final economic gate未放寬 |
| 最終T3 | 20:55:38–21:11:33，`182/182` cycles、`would_place=243`；實際覆蓋rising／neutral／falling切換。真實create/cancel、failed、ambiguity／unresolved、reconciliation failure、unknown、blocks、429、WS與read failure皆`0`，全程flat | Dry safety GO；graceful stop、runtime0、雙postflight position／orders`0/0`；證據 `logs/market_maker_t3_trend_loss_guard_final_20260827-211134.log` |
| 固定邊界短live | 21:41:59–21:47:37，約5分38秒；`12 completed / 5 round trips`、turnover `190.098420`、exact fee `0.02281181040`、net `+0.00500818960`、completed／flat-equity `+0.263452 / +0.263442 bps`、cover `1.21954`、max DD `0.023630`。21:47:29出現current unresolved cancel `1`及暫時position／ledger不一致，依hard gate立即停止；shutdown exact sync後為ambiguity／resolved／unresolved `1/1/0`、flat | **Hard-safety NO-GO／positive economic signal**；只到12 completed，不能promotion。Runtime0、雙postflight position／orders `0/0`，source已恢復dry-run；證據 `logs/market_maker_fixed_short_live_hard_stop_20260827-214750.log` |
| Cancel-vs-fill競態修復 | MM bootstrap才啟用exact terminal side-channel；legacy Grid cancel contract與production code不變。Exact `FILLED`保留原order資料、驗證ID／client alias、symbol／side／status與visible fill後清slot並refresh position，cancel不計success、當周期不補單；衝突replay、無法own proof或真正無proof仍fail closed，shutdown保留fill effect | **Offline/regression GO**。精準`7/7`、Lighter cancellation`31/31`、Grid targeted`6/6`、MM`319/319`；full repo`566`維持既知Grid `8F+4E` |
| Cancel-race修復後T3 | 22:31:15–23:02:40，`360/360` cycles、`would_place=404`；真實create/cancel、failed、ambiguity／unresolved、reconciliation failure、unknown、blocks、429、WS reconnect與account-read failure皆`0`，全程flat | **Dry safety GO**；graceful stop、runtime0、雙authenticated postflight position／orders`0/0`，source仍dry-run。證據 `logs/market_maker_t3_cancel_race_20260827-230233.log` |
| 固定邊界短live | 23:09:02–00:09:28，`735/735` cycles、21 completed／10 round trips、turnover`322.806772`、exact fee`0.03873681264`、gross`-0.001964`、net`-0.04070081264`、completed／flat-equity`-1.260841 / -1.260816 bps`、max DD`0.138115`。一度short`0.00030`長時間停在`soft_exit_hard_fallback`，其後自然flat；failed、ambiguity／unresolved、reconciliation failure、unknown、429與WS皆`0` | **Runtime/safety GO；volume/economic NO-GO**。未達30且gross未cover fee；session loss`0.04070081264 < 0.10`。Graceful stop、runtime0、雙postflight position／orders`0/0`，兩份config均恢復dry-run。證據 `logs/market_maker_cancel_race_short_live_20260828-000923.log` |

### 8.3 當前決策

**OPERATION STOPPED / PROCESS、OPEN ORDERS、POSITION皆0 / CANCEL-RACE SAFETY GO / 固定候選VOLUME＋ECONOMIC NO-GO / LONG-RUN禁止晉級。** Cancel-vs-fill修復沒有再出現uncertainty事故，但本場只完成21 fills，且自然flat後淨值／fee gate明確為負。`0.10 USDG` maker-exit budget雖把session loss限制在`0.04070081264`，`soft_exit_hard_fallback`長時間不動與後段虧損成交仍需離線鑑識；未釐清前不得直接重跑或進long-run。

VPS 同步與測試仍不在本階段。歷史事故與舊 fingerprint 僅見 [驗證歷史](market_maker_mvp_validation_history.md)，不能代替 fresh preflight。
