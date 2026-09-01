# Market Maker MVP 操作指南

本指南是單一 Lighter 永續合約 Market Maker MVP 的操作與安全來源。所有命令由 repository 根目錄執行；歷史逐輪證據另見 [驗證歷史](market_maker_mvp_validation_history.md)。

## 1. 不可放寬的規則

- 一般、passive unwind 與停機後人工 recovery quote 一律 `POST_ONLY`。唯一可成為 taker 的例外是**明確 opt-in 且預設關閉**的 active-unwind lane；該 lane 只能送 `reduce_only LIMIT + IOC`，禁止 market／FOK、加倉、反手、self-trade、跨帳戶互成交或任何形式的洗量。
- Live 每一輪都需操作者明確授權；縮短 dry-run 不等於沿用 live 授權。
- `active_unwind_enabled` 預設必須為 `false`；啟用 active lane 屬於新的 live 風險授權，不得從既有 maker-only 授權推定。
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
| Inventory unwind | `active_unwind_enabled` 預設 `false`。明確 opt-in 時須維持 `exclusive_symbol_control: true`、`cancel_on_shutdown: true`，並逐次核對**正值且已驗證**的 `taker_fee_rate`、`active_unwind_after_seconds`、`active_unwind_loss_trigger`、`active_unwind_max_slippage_ticks`、`active_unwind_max_attempts`、`active_unwind_confirmation_timeout_seconds`、`max_episode_loss_for_unwind`、`max_session_loss_for_unwind`；active deadline須晚於soft exit，maker-exit budget須為正，且必須滿足 loss trigger `<` episode cap `<` session cap `<` drawdown。目前沒有authenticated zero-taker-fee proof，因此不得以`0`啟用。 |
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
2. 未授權或非 active-unwind lane 的 taker、unknown order、未解決 uncertain mutation、reconciliation failure、account hard stop、429／reconnect storm、position／drawdown breach皆為 `0`。若明確啟用 active unwind，其 IOC fill 必須可歸屬到精確 active order ID、計入 taker fee 與 episode／session loss，且不得視為 maker fill。
   Cancellation 判定看目前狀態：`unresolved_cancellations=0`，且累計 `ambiguous_cancellations` 必須被同等數量的 `resolved_ambiguous_cancellations` 以 exact terminal proof 對消；未對消差額立即 hard stop。歷史已解決事件仍保留在 log，不得直接把累計 ambiguity 清零。
3. Exact fee cover `>= 1`，completed net／turnover與 flat equity／turnover都 `>= +0.02 bps`。
4. Graceful shutdown 後程序消失，兩次 authenticated postflight 都為 position／orders／unresolved `0/0/0`。

`max_session_loss_for_maker_exit`若非`0`，只允許已鎖定的`POST_ONLY reduce-only`退出使用，且必須採 exact trade P&L 與同 generation 的最後flat-equity證據較差者；證據缺失／過期時退回嚴格fee-aware價格，超限即hard stop。`bounded_economic_recovery`只保留給零mutation的dry-run／validation，明確不是GO。Live達到最小樣本後若gross不cover exact fee、completed net或flat-equity gate失敗，立即禁止新episode；若仍nonflat只保留reduce-only退出，authenticated flat後轉為`no_go`並停止。Maker-only基線為`0`；另行明確授權的active validation overlay為`0.05`，兩者不可混稱。

### 5.1 Active inventory unwind（default OFF）

Active unwind 是每個非零 inventory episode 的最後一道有界退出機制，不是一般報價或提高成交量的捷徑。Take-profit、stop-loss與time-limit是獨立barrier：每次trusted audit都先計算authenticated loss，無須等待soft-exit latch；time-limit只看inventory episode age；兩者均未觸發時才維持嚴格fee-aware profit exit，soft-exit latch後另可在maker budget內逐步追價。`active_unwind_enabled: false` 時，active lane 不存在，既有 maker-only 行為與 stranded guard 保持不變。

Live在flat準備建立新episode前，必須有fresh authenticated position／trade ledger／equity證據，並同時滿足 `remaining_session_loss_for_unwind >= max_episode_loss_for_unwind` 與 `remaining_drawdown > max_episode_loss_for_unwind`；缺失、stale或不足一律不開倉。Session等值可保留完整episode stop budget；drawdown因hard stop在抵達cap時生效，必須嚴格大於reserve。Dry-run沒有真實mutation，旁路帳戶economics檢查，不能把該旁路當成live proof。

明確 opt-in 後仍須遵守兩階段 barrier，不能在同一階段「先撤單、立刻 IOC」：

1. **Prepare phase**：先解決既有 uncertainty，以 exact terminal proof 撤除所有 managed orders，再用 authenticated symbol open-orders read 證明為 `0`。任何 uncertainty、foreign／unknown order、撤單 proof 缺失或 read failure都立即 fail closed；此階段不得送 active order。
2. **Fresh-truth phase**：撤單完成後重新取得 trusted BBO、authenticated position 與 account audit。一次性 truth token 必須綁定同一 episode、position方向／數量、audited fill generation、audit時間、book freshness及mutation／prepare generation；其中任一證據漂移就丟棄token並回到prepare，不得沿用舊價格或舊持倉。
3. **Active phase**：重新計算 barrier 後，只能送與持倉相反方向、數量不超過現有持倉的 `reduce_only LIMIT + IOC`。價格同時受 `active_unwind_max_slippage_ticks`、episode loss、maker+taker session loss及drawdown cap約束；attempt數與terminal confirmation分別受 `active_unwind_max_attempts`與`active_unwind_confirmation_timeout_seconds`限制。

Submission uncertain、identity／immutable-field不符、terminal proof timeout、position flip、非減倉fill、loss／slippage超限或attempt用盡都須 fail closed，且不得換 client ID盲重送。IOC的exact terminal結果可以是 no-fill、partial或full fill；每次結果後都要重新 authenticated position／audit，再決定是否仍有下一個有界attempt。

Telemetry 的 `active_unwind_success` **只表示該次 active order取得乾淨的 exact terminal proof**，不表示該order一定成交，也不表示episode已flat。`episode_flat_success`是authenticated trade ledger的episode零穿越數；只有後續authenticated position也為`0`，且同generation ledger／audit一致，才形成可恢復一般雙邊報價的flat checkpoint。Completed episode ledger保留最近100筆entry side、turnover、gross、maker／taker／exact fee、net、是否曾用active unwind及最後flatten lane；executor另保留最近100筆episode ID、active attempts及最後實際送出的loss／time trigger，不從no-fill attempt推論episode stop outcome。`episode_cap_blocked`同時計數可信entry reserve不足與active budget cap阻擋。

Maker fill markout以order-level增量成交量／成交額去除cumulative partial重複，涵蓋WebSocket order update、ordinary reconciliation與REST open-order sync帶回的可歸屬maker fill；active IOC會在fill分類前註冊為taker，live partial progress在有界快取淘汰時受保護，terminal後解除保護。記錄1／5／15／60秒side-signed markout及MAE／MFE，最多保留100筆。時間起點是該fill證據進入coordinator的monotonic observation time；reconciliation若只能提供延遲的聚合order proof，該筆代表observed order fill delta而非交易所逐筆trade或原始成交時間。這批telemetry目前只供離線量測；在fresh T3／live樣本證明coverage與分側門檻前，不啟用自動縮spread、擴side或pause controller。

Active unwind不豁免30 completed maker fills、fee cover或flat-equity gate；其 taker fee與實現損益必須完整計入。

任一 hard-safety 失敗立即停止並保留證據；不得為湊成交數而忽略。Economic NO-GO 只淘汰該參數候選，門檻不得降低。只有短 gate 完整 GO 才可從新的 fresh-flat session 啟動數小時 long-run。

Long-run 每 30 分鐘保存：completed fills／round trips、maker turnover、exact fee、net／bps、fee cover、flat-equity reconciliation、fills/h、maker USDG/h、episode ledger、分側markout／MAE／MFE、max drawdown／position、create/cancel／blocks、uncertainty／unknown／reconciliation、429／WS、CPU/RSS。自然 flat checkpoint 才能形成完整經濟證據。

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
| E | 單變因調參 | 一次只改 spread、size、skew、ratio 或 refresh 其中一項；每次重新驗證。Active unwind另走預設關閉的獨立rollout，不得和一般quote調參同場混合。 |

## 8. 本地 checkpoint（2026-09-01 entry-reserve short-live gate）

### 8.1 候選與邊界

| 項目 | 現行值 |
|---|---|
| 帳戶／市場 | 隔離 Lighter sub-account、Robinhood mainnet、BTC 獨占 |
| Source config | `config/market_maker/test_lighter_btc_mvp.yaml`，`dry_run: true`、`ping_pong_enabled: true`、`max_session_loss_for_maker_exit: "0"`，SHA-256 `9162163CC3B65153CF8FDFE34C3FCC92D28C5DEF0D3590B90F5E3A18984DF711` |
| Active validation configs | Ignored dry source與live copy目前皆為`dry_run:true`、episode／session cap=`0.075 / 0.10 USDG`，SHA `E60F3093E6FC2CC89875C2D79B27AC8E39C830F2FDBCC5CA1FA789573278C16D`。2026-09-01 short-live授權期間，live copy唯一semantic diff暫為`dry_run:false`、SHA`937016BF...1A257`；停機後已立即恢復。 |
| Runtime evidence scope | `OrderExpiry is invalid`根因已決定性定位並最小修復：只有IOC limit會補上SDK的`DEFAULT_IOC_EXPIRY`，POST_ONLY/GTT維持不送expiry；無pre/post-send provenance的同字串仍保守列為ambiguous。Fresh T3 GO後，final live已取得full-fill、partial-fill與residual active IOC的exact terminal proof，證明active execution path可用；其後session unwind loss cap按設計阻擋新IOC並安全停機。 |
| Quote／risk | `both / position-based ping-pong / 250 ticks / 0.00020 / max position 0.00040 / trend 60s/125 ticks / 1x cross / drawdown 0.50 USDG / 8 mutations/min` |
| Fee／exit | Maker-only基線：maker `1.2 bps`、soft exit `120s`、maker-exit budget `0`。Active validation overlay：另核對taker `3.5 bps`、maker-exit budget `0.05`；economic gate不變。 |
| Active inventory unwind | 程式與example config已加入per-episode barrier、passive chase及有界`reduce_only LIMIT + IOC` lane；**預設 `active_unwind_enabled: false`**。歷史live已證明full／partial／residual active unwind；2026-09-01 short-live又證明新程式會在live flat entry前保留完整episode cap，session餘額不足時不開新episode。這是prevention path的live proof，不是4h、active IOC或production promotion proof。 |
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
| Hummingbot參考後最小修正 | 只借用ping-pong語意：position非零時取消風險增加側，只留反向reduce-only；position先變flat仍須等待同generation的authenticated ledger flat。拒絕ping-pong搭配單邊模式或未啟用account audit。沒有引入全域fill delay、hanging tracker、額外ledger／executor或新mutation lane | **Offline GO／待T3**。精準`4/4`、MM`323/323`；full repo`570`維持既知Grid`8F+4E`。Grid production未修改，runtime0，本輪未live |
| Ping-pong正式T3 | 20:16:00–20:46:25，`348/348` cycles、`would_place=447`；真實create/cancel、failed、ambiguity／unresolved、reconciliation failure、unknown、429、WS及account-read failure皆`0`，全程flat | **Dry safety GO**；graceful stop、runtime0、雙postflight position／orders`0/0`。證據 `logs/market_maker_t3_ping_pong_20260828-204622.log` |
| Ping-pong固定邊界短live | 20:48:12–21:36:32；已證明多輪「maker entry→同側抑制→反向reduce-only maker exit→authenticated-flat barrier→恢復報價」。停止前`654/654` cycles、34 completed／16 round trips、completed turnover`485.272502`、exact fee`0.05823270024`、gross`-0.009290`、net`-0.06752270024`、`-1.391439 bps`、cover`-0.15953`、max DD`0.078506` | **Hard-safety與economic NO-GO**。21:34:02同一受管訂單在cancel terminal confirmation清slot後才收到延遲partial-fill／nonterminal WS update，被active-slot-only matching誤判unknown；counter`2`是同一事件被兩處觀察，不是兩張外部單。REST／audit證明ownership且10秒後收斂，但依gate仍停止。Runtime0、雙postflight orders0，保留BTC long`0.00020`；未獲本輪recovery授權。證據 `logs/market_maker_ping_pong_short_live_unknown_orders_20260828-213627.log` |
| 授權 maker-only recovery | 21:48以既有單向helper送出唯一`POST_ONLY + reduce_only SELL 0.00020`；helper取得flat terminal結果後正常退出 | **Recovery GO**。程序`0`；21:48:27與21:48:35兩次authenticated postflight皆BTC position／open orders=`0/0`、used collateral=`0`。Source與ignored live config均維持`dry_run:true`。 |
| Terminal replay ownership＋strict exit候選 | MM manager以runtime內、side與ID namespace綁定的terminal ownership proof吸收已知order延遲WS replay；amount／price／identity衝突、foreign order與REST active-after-terminal仍fail closed。Remaining low-watermark只下降，新增partial只refresh一次，重播不重複計fill；沒有shared adapter或Grid變更。同時只把本地loss-budget設為`0` | **Offline/regression + dry safety GO**。精準terminal replay`4/4`、order manager`87/87`、經濟分支`4/4`、MM`326/326`；full repo`573`維持既知Grid `8F+4E`。00:08:13–00:38:39 T3保存狀態`348/348`，真實mutation與全部hard-safety counters為`0`，全程flat；graceful stop、runtime0、雙postflight position／orders=`0/0`、used collateral=`0`。證據 `logs/market_maker_t3_terminal_replay_20260829-003823.log`；本輪未live。 |
| Terminal replay修復後固定邊界短live | 00:44:19–01:04:29，`277/277` cycles、32 completed／16 round trips、turnover`498.137240`、exact fee`0.05977646880`、gross`0.071120`、net`+0.01134353120`、completed／flat-equity`+0.227719 / +0.227708 bps`、cover`1.18977`、max DD`0.011374`。Session-scope證據中loss-budget exit與bounded recovery皆`0`；有可信authenticated盈餘時使用既有soft exit。Failed、ambiguity／unresolved、reconciliation failure、unknown、429、WS與account hard stop皆`0` | **Short-live safety／economic GO**。30 fills且flat時已達完整經濟gate；01:04:09一次position snapshot unavailable令runtime fail closed，account audit仍healthy，停止窗口中已進入的maker episode自然完成並由01:04:29 final audit確認flat。Terminal replay未自然出現，因此live只證明正常路徑，未宣稱已證明replay路徑。Evidence `logs/market_maker_terminal_replay_short_live_final_20260829-010429.log`；runtime0、雙postflight position／orders=`0/0`、used collateral=`0`、equity`299.383977`，source與fresh live copy均已恢復dry-run。 |
| 固定候選4小時long-run | 18:27:26–18:34:02；18:33:36.467 status曾顯示current `unresolved_cancellations=1`，舊monitor依hard gate停止；18:33:36.557 adapter已取得同一cancel的exact terminal history proof並清marker，相差約90ms。Final audit為`81/81` cycles、position／orders=`0/0`、公開hard-safety counters全為`0`，completed fills=`0` | **依當時規則仍記Operational NO-GO；鑑識確認是in-flight confirmation觀測誤報，不是策略／經濟失敗**。Runtime0、雙postflight `0/0`；本輪無經濟樣本，不回溯改判GO。證據 `logs/market_maker_longrun_hard_stop_20260829-183355.log`、`logs/market_maker_longrun_hard_stop_final_20260829-183527.log`、`logs/ExchangeAdapter.log`。 |
| Cancel confirmation status truth修復 | 公開`unresolved_cancellation_count`只排除同symbol／同order、受管slot仍為`CANCELING`且尚未標記uncertain的exact-confirmation窗口；真正`UNCERTAIN_CANCELLATION`、`cancellation_uncertain`、adapter-only或mismatched key仍計數。Adapter registry、timeout／retry、`has_uncertain_state`、下單阻擋、shared Lighter與Grid均未改 | **Offline + T1 GO；long-run未重新授權**。OrderManager`88/88`、MM`327/327`；full repo`574`維持既知Grid `8F+4E`。18:53:59–18:59:10 T1 final `60/60`，真實mutation與全部hard-safety counters為`0`、全程flat；runtime0、雙authenticated postflight position／orders=`0/0`。證據 `logs/market_maker_t1_cancel_confirmation_metric_20260829-185910.log`。 |
| 修復後固定候選4小時long-run | 19:05:37–20:57:55；唯一runtime依授權執行，後因cancel uncertainty／reconciliation failure觸發hard stop。Shutdown後runtime0，但authenticated狀態為BTC short`0.00020`、open orders`1`、used collateral`15.513460` | **Hard-safety NO-GO／停止後帳戶未收斂**。Fresh live copy已恢復`dry_run:true`；兩次authenticated postflight均exit0且signer／wallet／authenticated reads PASS，但皆為positions`1`（short`0.00020`）／open orders`1`。未授權recovery或重跑，故保持runtime0並保留原狀。證據 `logs/market_maker_longrun_restart_hard_stop_20260829-205755.log`、`logs/market_maker_longrun_restart_checkpoints_20260829.md`。 |
| E2ag手動flat與cancel fail-fast修復 | 使用者於21:05自行平掉E2ag殘留short並清除殘單；不得歸因策略。MM-only修復在genuine cancel uncertainty形成後立即停止同cycle其餘side mutation，adapter／Grid不變。OrderManager`88/88`、MM`327/327`；full repo`574`維持既知Grid `8F+4E` | **Offline GO**；未放寬uncertainty gate。Fingerprint `BB2C2BBC...A1051DC0`。 |
| Cancel fail-fast修復後T3 | 21:28:54–22:00:56，final `366/366` cycles、`would_place=594`；真實create/cancel、failed、ambiguity／unresolved、reconciliation failure、unknown、429、WS及account failure皆`0`，全程flat | **Dry safety GO**；graceful stop、runtime0，雙authenticated preflight position／orders=`0/0`、used collateral=`0`。證據 `logs/market_maker_t3_cancel_failfast_20260829-220053.log`。 |
| 同固定候選4小時long-run重跑 | 22:01:45–22:46:19。先完成3 round trips／6 completed fills；completed net `+0.022872 bps`、flat-equity `+0.022956 bps`、fee cover`1.01906`，但未達30。22:43 BUY cancel ambiguity由同單maker fill exact-resolve並形成long`0.00020`；其後reduce-only SELL submission因`invalid nonce`被保守列為uncertain，`reconciliation_failure=2`，之後無新增mutation | **Hard-safety NO-GO／economic sample incomplete**。Final `543/522/21` cycles，cancel ambiguity／resolved／current unresolved=`1/1/0`、unknown／429／WS=`0`。Runtime0、open orders`0`，但authenticated postflight仍為BTC long`0.00020 @ 77859.1`、used collateral`15.571820`；source與live copy均恢復`dry_run:true`，未獲recovery授權。證據 `logs/market_maker_longrun_hardstop_reconciliation_20260829-224632.log`。 |
| Invalid-nonce修復、T3與首輪4h | 精確`21104 / invalid nonce / {}`在MM opt-in下才視為definitive not-submitted，hard-refresh後只允許下一cycle重試；其他結果仍fail closed。23:15:28–23:49:40 T3為`390/390`且全部hard-safety counters`0`。23:50:37–03:50:37 long-run為`2789/2789`、6 completed／3 round trips、completed net`+0.0240908 bps`、cover`1.02008`，但final short`0.00020`且少於30 | **T3安全GO；4h economic incomplete_nonflat**。依授權用單向`BUY LIMIT + POST_ONLY + reduce_only`於`78139.1` exact fill回到flat；不得把recovery計入場內經濟樣本。Evidence `logs/market_maker_t3_invalid_nonce_20260829-234937.log`、`logs/market_maker_long_run_4h_final_20260830-035037.log`。 |
| Stranded-soft-exit guard與fresh T3 | Soft-exit latch後若正確方向reduce-only實際報價超出normal half-spread加1 tick，先撤managed orders再fatal stop；strategy算價、economic gate與maker-only限制不變。MM`333/333`；full repo`580`維持既知Grid`8F+4E`。04:20:10–04:53:16 T3為`379/379`、`would_place=601`，真實mutation及全部hard-safety counters`0` | **Offline／dry safety GO**。Evidence `logs/market_maker_t3_stranded_guard_20260830-045312.log`；fingerprint `BD7F8A8C...BC41F48`。 |
| 第一次intended guard stop、recovery與再T3 | 04:54:56 live啟動，05:49 guard按設計停止：`705/705`、failed與其他hard-safety counters`0`，short`0.00020`、managed orders`0`。授權maker-only recovery exact fill後flat；05:57:11–06:28:11 fresh T3 final為`354/354`且全部hard-safety counters`0` | **Guard安全GO；樣本無經濟判定**。Stop/recovery/T3 evidence為`logs/market_maker_long_run_stranded_stop_20260830-055234.log`、`logs/market_maker_recovery_20260830-055454.txt`、`logs/market_maker_t3_post_recovery_20260830-062753.log`。 |
| 第二次intended guard stop與最終recovery | 06:29:03重新啟動；07:29:09同一guard決定性重現。Final `775/775`、1 maker fill／0 completed、economic `incomplete_nonflat`、short`0.00020`、orders`0`、runtime0；failed、ambiguity／unresolved、reconciliation failure、unknown、non-maker、429、WS與account hard counters全`0`。授權單向BUY `POST_ONLY + reduce_only`於`78215.5`取得exact terminal fill | **安全停止／不再同候選自動重跑**。07:34雙authenticated postflight position／orders=`0/0`、used collateral=`0`、equity`299.299693`；兩份config均恢復`dry_run:true`。Evidence `logs/market_maker_long_run_stranded_stop_repeat_20260830-072909.log`（SHA `63F75DBC...5C314`）、`logs/market_maker_recovery_repeat_20260830-073412.txt`（SHA `BBF8CD45...27429`）。 |
| Active-enabled T3與首次live canary | 15:54:01–16:26:57 dry T3為`374/374`、`would_place=566`，真實mutation與全部hard-safety／active counters`0`。16:31:00 live啟動；第三個short episode進入`active_ready`約`478.516s`，position refresh累計`61`，但17:01停止前active attempts仍為`0`，沒有送出IOC；final `387/387`、failed`0`、5 unique／4 completed／2 round trips、economic `incomplete_nonflat`、short`0.00020`、orders`0` | **Dry safety GO；live active lane未獲證明**。Live evidence `logs/market_maker_active_unwind_live_active_ready_stall_final_20260830-170100.log`，SHA `E8C084F7...6FC1A`。 |
| Active-ready stall recovery、最小修復與fresh T3 | 依授權只用單向`BUY LIMIT + POST_ONLY + reduce_only`，order `844424883363107` exact fill `0.00020 @ 78045.8`回到flat，未用IOC／market／taker；recovery evidence `logs/market_maker_active_unwind_stall_recovery_20260830-1704.txt`，SHA `EDF8DED1...F4F95`。根因是armed truth token被一般debounce吞掉，position update清token後又反覆reprepare；修復只讓armed token繞過一次debounce，並在既有generation仍有效時先fresh BBO／REST position／audit後re-arm，失敗仍fail closed。MM`370/370`；full repo`617`維持既知Grid `8F+4E`。17:18:44–17:51:06 fresh T3 final `370/370`、`would_place=580`，真實mutation及全部hard-safety／active counters`0` | **Offline／dry safety GO；等待新的明確live授權**。T3 evidence `logs/market_maker_active_unwind_fix_t3_20260830-175102.log`，SHA `64DF92235D4A5BFD143575FA8F023960448C3FA81F92F938BD0E6121C0FE9754`；graceful stop、runtime0，17:51雙authenticated postflight position／orders=`0/0`、used collateral=`0`、equity`299.278783`。 |
| Truth lifecycle修復後60分鐘live canary | 18:20:29–19:20:55，final `756/756` cycles、failed`0`、10 completed maker fills／5 round trips、turnover`156.112160`、gross`0.004240`、exact fee`0.01873345920`、net`-0.01449345920`、completed／flat-equity`-0.928400 / -0.928435 bps`、cover`0.226333`、max DD`0.016619`。Final flat、orders`0`、runtime`0`；全部hard-safety counters、taker fills及active attempts／success／no-fill／partial／ambiguous／blocks皆`0` | **Runtime／hard-safety GO；economic collecting；active IOC path仍未證明**。10 fills未達30-fill gate，負bps不得判economic NO-GO；未觸發active lane亦不得當作IOC proof。Evidence `logs/market_maker_active_unwind_live_60m_final_20260830-192131.log`，SHA `74B673BC5E5BB3E01AD6AFCA04D437F6A2873EA4E3BB778B7EF7A19754C7B721`；雙authenticated postflight皆position／orders=`0/0`、used collateral=`0`、equity`299.264289`，live copy已恢復`dry_run:true`。 |
| Final 4h active-unwind evidence validation | 19:50:03啟動；19:57:59首個time-triggered active episode準備`BUY 0.00020 @ 78127.5 / reduce_only / IOC`，projected episode／session loss皆`0.010643589`，未越過position、loss、slippage或drawdown cap。Adapter收到`OrderExpiry is invalid`後無法取得exact terminal proof；final `104/106/2` successful／total／failed、active attempts／ambiguous=`1/1`、reconciliation failure=`1`，taker fills／unknown／429／WS／account read failure皆`0` | **Hard-safety NO-GO；8分鐘停止；economic unavailable**。Runtime0、authenticated open orders`0`，但short`0.00020 @ 78111.0`仍在；雙postflight均reads PASS且同一nonflat狀態。Live copy已恢復dry-run；沒有本輪flatten授權，故未做更多mutation。Evidence `logs/market_maker_final4h_active_unwind_hard_stop_20260830-195759.log`，SHA `AF69439450DD842E51139B51367E630AA19D615047FF6C683047505BCFB767D7`。 |
| IOC expiry最小修復與fresh T3 | Lighter SDK的IOC limit需要sentinel expiry；adapter原先完全未傳`order_expiry`，SDK／order-submission path回覆`OrderExpiry is invalid`且沒有terminal proof。修復只對IOC加入`lighter.SignerClient.DEFAULT_IOC_EXPIRY`，POST_ONLY/GTT不變；無submission provenance的錯誤仍fail closed。Focused`3/3`、integration`33/33`、MM`371/371`、Grid POST_ONLY`3/3`；full repo`618`維持既知Grid `8F+4E`。21:14:18–21:47:45 T3為`383/383`、`would_place=537`，真實mutation及全部hard-safety counters`0` | **Offline／dry safety GO**。Evidence `logs/market_maker_t3_ioc_expiry_20260830-214745.log`，SHA `FEF6B22B44F25D099FDFB0083F71F5E1185735C96939A2632BB646E8636A6BF5`；runtime0，雙preflight position／orders=`0/0`、used collateral=`0`、equity`299.146456`。 |
| IOC修復後final 4h live rerun | 21:48:54啟動；22:22 checkpoint為`465/465`、9 maker／1 taker fills、5 completed trips、flat且雙orders，active first full-fill取得exact proof。其後另一episode取得partial IOC與residual IOC exact proof。22:49:54第10輪long`0.00020`達active deadline，但新IOC會超過session unwind loss cap`0.10`（餘額僅`0.003245`），因此在submission前block並fatal stop。Final `832/832`、failed`0`、18 unique／17 completed maker、2 taker、9 trips；turnover`283.762524`、gross`-0.055316`、exact fee`0.04129704108`、net`-0.09661304108`、`-3.404715 bps`、cover`-1.33947`。Active attempts/success/partial/blocks=`3/3/1/1`；ambiguity／unresolved／reconciliation failure／unknown／non-maker／429／WS／account hard counters皆`0`，3次短暫account reads均恢復，max DD`0.102764 < 0.50` | **Active IOC execution GO；session-cap safe stop；4h與economic皆未完成**。Final long`0.00020 @ 78707.9`、orders`0`、runtime0；雙authenticated postflight一致為long`0.00020`、orders`0`、used collateral`15.741580`、equity`299.047812`。Evidence `logs/market_maker_final4h_session_loss_cap_stop_20260830-224954.log`，SHA `FEC3343ED3D703482BC9F0DDFA46DFD0A1A6369B545093D31BC66E8E8E7BBF20`；metrics SHA `C56F9F436C2D4F0E00A6AA22A57998BAE3BECC4BB4E2A4568577919F413F4EC8`。 |
| Session-cap事件後P0／P1與markout基礎 | Live entry前保留完整episode cap並要求session／drawdown雙headroom；episode `<` session `<` drawdown；loss／time barrier與soft-exit latch解耦。Live economic failure先鎖新episode、nonflat只允許reduce-only、authenticated flat後`no_go`；audit狀態在cycle lock內發布，failure同步阻斷新cycle。新增bounded episode economics／execution history、maker／taker fee與final close lane、WS＋ordinary reconciliation＋REST sync增量markout，並修正maker eligible-hour turnover不再含active taker。Focused`328/328`、MM`400/400`；full repo`647`維持既知Grid `8F+4E`。 | **Code-only／offline；尚未fresh T3或live revalidation**。Active預設OFF，兩份ignored overlay仍`dry_run:true`；未連線交易、未做帳戶mutation，dynamic quote controller仍停用。 |
| Session-cap hardening後fresh T3 | 21:36:56–22:07:38以active dry source與CLI雙重dry-run執行。Final uptime`1844.766s`、`351/351` cycles、failed`0`、`would_place=388`、reconciliation`351/0`；真實create／cancel、account read failure、unknown、ambiguity／unresolved、blocks、429、WS、active與episode-cap counters全`0`，全程flat／audit healthy。Entry-admission顯示dry-run旁路；markout coverage包含WS、ordinary reconciliation與REST sync，零fill故retained events`0`。 | **Dry safety GO；live未授權**。單次Ctrl+C後runtime0；22:08雙authenticated postflight position／orders=`0/0`、used collateral`0`、equity`299.101926`。Evidence `logs/market_maker_t3_episode_cap_markout_20260831-220738.log`，SHA `762CB7B173A09CCFFFC685E9CAD39F75C03601EDDA58794125AF267E3BC1C152`。1000-line handler於uptime`574.641s`重置，raw檔只保留後段；前段精確monitor checkpoints與限制另存同名metrics檔，不得描述為完整逐行transcript。 |
| Entry-reserve hardening後60分鐘short-live gate | 02:24:45–02:47:56；final uptime`1394.25s`、`315/315` cycles、16 completed maker fills／8 trips、taker`0`、turnover`252.524840`、gross`-0.002040`、exact fee`0.03030298080`、net`-0.03234298080`、completed／flat-equity`-1.280784132 / -1.280784892 bps`、cover`-0.06732011`、max DD`0.035207`。第8輪authenticated flat後，remaining session unwind budget`0.067657`已小於新episode完整reserve`0.075`，entry admission在任何新下單前block。Failed、ambiguity／unresolved、reconciliation failure、unknown、429及active counters皆`0`；1次read failure與1次WS reconnect均有界恢復。 | **P0 live prevention proof GO；short-live promotion NO-GO；4h未啟動**。不可用重置session accounting或放寬`0.10` cap繞過。未達30 fills，正式economic state仍`collecting`，負值只是不利訊號。單次Ctrl+C後runtime0；02:52雙authenticated postflight position／orders=`0/0`、used collateral`0`、equity`299.069583`，兩份config均恢復`dry_run:true`。Evidence `logs/market_maker_short_live_entry_reserve_nogo_20260901-024756.log`，SHA`BC60B6C4...6F1109`；metrics SHA`057C868C...F81F79`。 |

### 8.3 當前決策

最新live判定為：**GATE C HARD NO-GO / 4H NOT STARTED / RECOVERY COMPLETE / ACCOUNT FLAT**。2026-09-02 Gate C shadow live在uptime `845.110s`因`soft exit is stranded outside the normal passive quote band by the economic gate`觸發設計中的fatal guard；本場只有8 completed maker fills／4 trips，未達30且stop時未authenticated-flat，不得將已完成子集的正數結果或shadow proxy用於promotion。取得明確授權後，02:30:55–02:31:09只用單向`BUY LIMIT + POST_ONLY + reduce_only 0.00020`於`76987.8`取得exact terminal fill；雙authenticated postflight皆為BTC position／orders=`0/0`、used collateral=`0`，runtime=`0`。Recovery只屬帳戶清理，不計入Gate C economics，也不解除Hard No-Go。兩份ignored overlay維持語意相同的`dry_run:true`、`quote_controller_mode:"fixed"`、`toxicity_feature_window_seconds:120`、`toxicity_min_samples:20`、`active_unwind_enabled:false`，source／local-live-copy SHA分別為`4467D479...94DDFE`／`164C883E...07BAB9`。

VPS 同步與測試仍不在本階段。歷史事故與舊 fingerprint 僅見 [驗證歷史](market_maker_mvp_validation_history.md)，不能代替 fresh preflight。

## 9. Toxicity-aware entry controller rollout

本checkpoint新增bounded external-book feature store、authenticated fill-role attribution、`fixed/shadow/active` controller、entry-only arbiter、controller telemetry與本地offline analyzer。它沒有改Grid production code或shared adapter；OrderManager仍是唯一mutation authority，normal quote仍是`POST_ONLY`。Example YAML繼續保持`dry_run:true`、`active_unwind_enabled:false`、`quote_controller_mode:"fixed"`。

Controller硬邊界：

- 只處理flat inventory下的普通maker entry；non-flat或任何`reduce_only`直接bypass。
- 只能向外widen或移除原本存在的side；不能縮spread、加size、改reference／reservation、改TIF或新增side。
- Economic stop、entry reserve、RiskManager、inventory unwind與uncertain／reconciliation fail-closed均有優先權。
- Active feature未ready、stale、invalid或feature pipeline exception時，flat entry關閉；non-flat exit仍可收斂。
- Markout event同時保存raw BBO與own-size-subtracted external mid；side／authenticated summaries、entry feedback與promotion analysis只接受external markout。External book或managed-order truth不可信時不補值，另以coverage／telemetry error揭露；v1 active仍禁止使用feedback，shadow只供校準。

本地分析只讀既有log／metrics，不連線交易所：

```powershell
.\.venv\Scripts\python.exe .\scripts\analyze_market_maker_strategy.py <metrics-or-log> --json-output report.json --markdown-output report.md
```

Shadow輸出是counterfactual proxy，不是queue-fill backtest。Analyzer只把external markout納入策略統計；raw markout僅供diagnostic。若placement後、實際fill前出現shadow block／reprice、controller unavailable或entry不再適用，該fill保守列為`indeterminate`。舊E2ay只有16筆未帶authenticated role join的markout，所以必須維持`pending=16 / indeterminate=16`，不能回溯推斷entry／exit。Periodic checkpoint中的completed episodes依`(session_id, episode_sequence)`合併；無穩定identity的舊schema只讀final snapshot，避免跨checkpoint重複計數。

Promotion順序固定：

1. Gate A：`fixed` 30分鐘dry T3，真實create／cancel為0、hard counters全0，並證明既有quote parity。
2. Gate B：`shadow` 30分鐘dry T3，final desired／would-place與fixed一致，feature readiness、decision history及CPU／RSS有界。
3. Gate C：另取明確live授權後才做shadow live；實際仍送fixed quote，至少30 completed maker fills且authenticated natural flat，產出calibration report但不得稱收益證明。
4. Gate D：依authenticated entry markout選較差一側，只開單側widening、不開blocking；base spread、size、mutation、unwind caps與另一側均不變，完整economic/hard-safety gate照舊。
5. Gate E：只有單側active canary完整GO後才測雙側widening；side blocking仍是另一個單變因。
6. Gate F：至少2–3次彼此獨立的fresh-flat short sessions通過後才做4小時；4小時GO後才考慮24小時／VPS。

本checkpoint沒有執行Gate A–F、network、T3、live、flatten或account mutation，也不授權下一輪。E2ay維持「P0 prevention proof GO／short-live promotion NO-GO／4h未啟動／正式30-fill economics incomplete」。

### Review remediation checkpoint（2026-09-01，code-only）

針對commit `370cac4c`的3項P1與2項P2 review findings，採用以下有界修正：

- `rms_1s_move_*`改為依相鄰有效樣本的實際`delta_t`計算每秒realized variance rate：`sqrt(sum(move_ticks^2) / sum(delta_t_seconds))`；不規則／5秒cadence不再被誤當成1秒取樣，既有reset gap仍不跨越。
- Fill telemetry保留legacy raw `markout_*`／MAE／MFE，並新增raw／external start mid及各horizon mid／markout。External reference沿用feature store的managed own-size subtraction；不可信order state、invalid external book或缺值不會回填。Controller feedback、metrics summaries與offline analyzer只讀external markout。
- Trade-ID replay proof與order-role binding各使用8,192筆bounded LRU。Trade ID只淘汰authenticated current 100-trade page之外的舊identity，watermark仍拒絕out-of-order新證據；role binding永久pin authenticated open orders、current trade page與尚未flat的open episode，且只有OrderManager已有exact terminal proof的最舊order才具淘汰資格，不能用「不在open orders」推定terminal。若全部候選仍被pin或沒有exact-terminal候選，維持fail closed且不提交局部registry變更。Synthetic long-run涵蓋4,000個不同filled order IDs／2,000 episodes、retained old-trade replay no-op，另覆蓋實際被淘汰trade的watermark拒絕、terminal／nonterminal／open／episode pinning與cap失敗atomicity；runtime completed ledger仍只保留最近100筆。
- 每筆completed episode新增穩定`session_id`、`episode_sequence`、authenticated `opened_at`／`closed_at`與`entry_side`；offline analyzer可跨periodic checkpoints去重合併完整分布。
- Controller history保存各side的base／shadow／applied price，並把shadow reprice與`entry_applicable`變更視為transition；total／retained coverage亦明確輸出。Analyzer若在placement至fill時間窗內看到後續reprice、block、unavailable或inapplicable，或輸入checkpoint未完整覆蓋已報告的history total，一律不使用placement-only結論，改列`indeterminate`。

Offline驗證：review-focused整合`333/333`、全部Market Maker `493/493`；full repo `740`維持原HEAD既知Grid／Lighter lifecycle baseline `8F+4E`，失敗／錯誤名稱完全一致，沒有新增回歸。兩次獨立read-only runtime review均未發現blocking finding。既有OrderManager `_terminal_orders`／`_known_order_ids`仍是session-unbounded proof source，24h gate容量可接受，但在宣稱indefinite bounded-memory production前必須另案治理。

本checkpoint未執行fresh fixed T3、shadow T3、network、live、flatten或任何帳戶mutation，維持code-only；其後仍須依Gate A、Gate B取得fresh runtime evidence，active controller live不因本次程式修正自動promotion。

### Gate A／B fresh dry T3（2026-09-02）

- **Gate A fixed GO**：23:29:04–23:59:15，uptime `1803.953s`、`346/346` cycles、failed `0`、`would_place=432`；真實create／cancel、reconciliation failure、unknown、ambiguity／unresolved、mutation block、429、WS reconnect、account-read、controller與markout error全為`0`，全程flat／audit healthy。Fixed base／applied quote保持一致，analyzer history `331/331 complete`；history上限`200`後RSS約`144–146 MiB`。Raw evidence `logs/market_maker_t3_controller_fixed_20260901-235915.log`，SHA `F5DF8799A8C173C99E0396AF72B289711F9C3D330A6A80DE29862FF4C897B168`。
- **Gate B首次readiness NO-GO，非hard-safety事故**：00:01:01–00:05:03為`46/46`且全部hard counters為`0`，但`refresh_interval_ms=5000`搭配60秒retention最多只保留約13筆，無法達到`toxicity_min_samples=20`，controller會永久warming。只把feature retention由60秒擴為120秒，保留20筆門檻及既有5／15／60秒公式；focused config／feature／telemetry `47/47`通過。Diagnostic evidence `logs/market_maker_t3_controller_shadow_readiness_nogo_20260902-000503.log`，SHA `565D80F67A6E9BDEBDFB121B0630503EFFF874D46B05FCBBEB680F144A75386C`。
- **Gate B repaired shadow GO**：第一段uptime `902.875s`、`173/173`；PTY在累積約10 MB輸出後結束，程式最後狀態沒有exception、failed或hard-counter異常，故保存為harness interruption，fresh authenticated restart preflight仍為position／orders=`0/0`。以輸出重導續跑第二段uptime `1272.703s`、`243/243`；合計有效時間 `2175.578s`（約36分16秒）、`416/416`、failed `0`、`would_place=482`，真實create／cancel及全部hard counters仍為`0`。兩段feature約99秒後ready並穩定於24–25 samples；retained history達`200`後保持封頂，第二段400個保留base／applied price欄位零差異，status約299 KiB、RSS約146 MiB。Raw evidence為`logs/market_maker_t3_controller_shadow_part1_pty_limit_20260902-002315.log`（SHA `48A821E7034A5C1E863E83597C07A5E47C58BB7B65D60982FA1C96EBAA87C7AF`）及`logs/market_maker_t3_controller_shadow_part2_20260902-004655.log`（SHA `098E6555F84A212EEE2A3247534741F1CFCA767C27F9AFD23D332762A3FD2D21`）。
- Part 2單次Ctrl+C後runtime為`0`；兩次authenticated postflight皆為BTC position／orders=`0/0`、used collateral`0`、equity`299.069583`。本輪全程CLI強制dry-run，沒有live、fills、flatten或帳戶mutation。Analyzer history fixed／shadow-part1／shadow-part2分別為`331/331`、`153/153`、`223/223 complete`；因此Gate A／B只證明runtime safety、shadow parity與資源有界，**不證明queue fill或收益**。
- Current rollout state：**Gate A GO / Gate B GO / Gate C HARD NO-GO / Gate D–F NOT RUN；recovery完成且account flat**。未完成根因修復與相稱offline／fresh dry T3前不得重跑；active controller不因既有dry T3或本次recovery自動promotion。

### Gate C shadow live hard stop（2026-09-02）

- 01:51:12–02:05:12，uptime `845.110s`、`198/198` cycles、failed `0`；真實create／cancel=`27/18`且全成功，maker／taker fills=`9/0`，completed=`8`、round trips=`4`。兩次短暫stale-book pause均撤空managed quote、在下一個authenticated audit checkpoint內恢復；reconciliation failure、unknown、ambiguity／unresolved、mutation block、429、WS reconnect、account read、controller、markout與active counters全為`0`。Shadow history `128/128 complete`，base／applied parity零差異。
- Completed turnover／gross／exact fee／net=`123.670880 / 0.022800 / 0.01484050560 / +0.00795949440`，completed net=`+0.6436029565 bps`、fee cover=`1.5363357971`，max DD=`0.004453 < 0.50`。但只有`8/30` completed fills且第五個episode未flat，正式economic gate未形成。External markout retained／merged／pending=`9/9/0`，9筆shadow proxy全部因後續reprice或exit eligibility保守列為`indeterminate`，不能用來選side。
- 02:05:12 fatal stop原因為`strategy hard stop: soft exit is stranded outside the normal passive quote band by the economic gate`。Shutdown後runtime／orders=`0/0`，兩次authenticated postflight一致為BTC short `0.00020 @ 77207.4`；未做recovery mutation。Raw evidence `logs/market_maker_gate_c_shadow_live_stranded_stop_20260902-020512.log`，SHA `6B96B04ECFA8102773355DC1B372CCFBFCE22D8E7CCF5428DAAE5D929B6D7519`；analyzer history `128/128 complete`。
- 取得明確授權後，02:30:55–02:31:09以未變更的既有helper（SHA `2303F87C...172`）執行單向maker-only recovery；order `844424878317012`為`BUY LIMIT + POST_ONLY + reduce_only 0.00020 @ 76987.8`，exact history為filled `0.00020`、remaining `0`。02:31:26／02:31:36雙authenticated postflight皆position／orders=`0/0`、used collateral=`0`、equity=`299.117762`；runtime=`0`，兩份config仍為`dry_run:true`。Sanitized evidence `logs/market_maker_gate_c_recovery_20260902-0231.txt`，SHA `A22999B200AB1384B9B439DBEB805DC36D11559057EB06974EECE0365E2F1EAC`；recovery不計入Gate C economics。
- 判定：**Gate C hard No-Go；4h未啟動；recovery完成且account flat**。這是設計中的安全停止，不得在live內熱修、放寬economic gate或直接續跑。後續順序為根因修復→offline／fresh dry T3→從Gate C重跑，不能跳過Gate D–F。
