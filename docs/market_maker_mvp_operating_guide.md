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
| Entry controller | Checked-in example的安全預設為`quote_controller_mode: "fixed"`、`toxicity_apply_bid/ask: false`、`toxicity_max_extra_spread_ticks: 0`、`toxicity_block_threshold_ticks: "0"`、`toxicity_use_markout_feedback: false`、`active_unwind_enabled: false`及`dry_run: true`。Protective預設為outward threshold `1` tick、minimum interval `5000 ms`；active profile只能存在ignored/local overlay或明確sanitized candidate，不得打開source default。 |
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

Active unwind 是每個非零 inventory episode 的最後一道有界退出機制，不是一般報價或提高成交量的捷徑。Take-profit、stop-loss與time-limit是獨立barrier：每次trusted audit都先計算authenticated loss，無須等待soft-exit latch；time-limit只看inventory episode age；兩者均未觸發時才維持嚴格fee-aware profit exit，soft-exit latch後另可在maker budget內逐步追價。`active_unwind_enabled: false` 只停用 IOC lane；executor 仍擁有所有 non-flat episode，並在soft latch後依authenticated episode／session／drawdown邊界產生`POST_ONLY + reduce_only` passive intent。舊stranded fatal不再是active-off的終局；缺少可信economics時不得放寬normal strict exit，active-on且抵達deadline仍無可信證據時才fail closed。

Live在flat準備建立新episode前，必須有fresh authenticated position／trade ledger／equity證據，並同時滿足 `remaining_session_loss_for_unwind >= max_episode_loss_for_unwind` 與 `remaining_drawdown > max_episode_loss_for_unwind`；缺失、stale或不足一律不開倉。Session等值可保留完整episode stop budget；drawdown因hard stop在抵達cap時生效，必須嚴格大於reserve。Dry-run沒有真實mutation，旁路帳戶economics檢查，不能把該旁路當成live proof。

明確 opt-in 後仍須遵守兩階段 barrier，不能在同一階段「先撤單、立刻 IOC」：

1. **Prepare phase**：先解決既有 uncertainty，以 exact terminal proof 撤除所有 managed orders，再用 authenticated symbol open-orders read 證明為 `0`。任何 uncertainty、foreign／unknown order、撤單 proof 缺失或 read failure都立即 fail closed；此階段不得送 active order。
2. **Fresh-truth phase**：撤單完成後重新取得 trusted BBO、authenticated position 與 account audit。一次性 truth token 必須綁定同一 episode、position方向／數量、audited fill generation、audit時間、book freshness及mutation／prepare generation；其中任一證據漂移就丟棄token並回到prepare，不得沿用舊價格或舊持倉。
3. **Active phase**：重新計算 barrier 後，只能送與持倉相反方向的 `reduce_only LIMIT + IOC`。一般情況送出現有持倉量；若partial fill留下小於交易所最小下單量的殘量，送單量改為`max(order_size, abs(position))`，但價格、fee、episode／session loss及drawdown投影仍只按實際殘量計算，`reduce_only`保證flat後剩餘量取消而不反向開倉。價格另受 `active_unwind_max_slippage_ticks`約束；attempt數與terminal confirmation分別受 `active_unwind_max_attempts`與`active_unwind_confirmation_timeout_seconds`限制。

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
| Gate C inventory-owner修復與本輪T3→30m short-live | Executor在active-off時仍擁有non-flat passive exit，surplus／episode／session／drawdown邊界使用authenticated evidence；metrics改用單調event sequence，analyzer只接受`placement < decision < fill`且只以authenticated non-active entry作分母。Live稽核另抓出sub-min residual退出量缺口，修為`max(order_size, abs(position))`而經濟投影仍採實際殘量。修復後T3 `345/345`、would-place`443`、真實mutation與全部hard counters`0`；30m live `369/369`、6 maker／0 taker fills、3 round trips、flat clean stop。場後review再加入per-runtime sequence ID、virtual lifecycle state fold及long-boundary非正分母fail-closed guard；此後續hardening只有offline regression，沒有重開live。 | **Hard-safety／inventory ownership GO；Gate C promotion與economics仍INCOMPLETE（6/30）；4h未啟動**。第三輪flat後remaining session budget`0.066670 < 0.075`，entry admission按設計封鎖；不得重置session或放寬cap湊樣本。雙postflight position／orders=`0/0`、used collateral`0`，兩份overlay均恢復`dry_run:true`。 |

### 8.3 當前決策

最新live判定為：**GATE C REMEDIATION HARD-SAFETY GO / PROMOTION + ECONOMICS INCOMPLETE (6/30) / 4H NOT STARTED / ACCOUNT FLAT**。2026-09-02完成inventory-owner根因修復、sub-min residual補強、fresh T3及完整30分鐘shadow short-live。Final live uptime`1806.282s`、`369/369` cycles、6 completed maker fills／3 trips、taker與active attempts均`0`；completed turnover／gross／exact fee／net=`92.411840 / -0.022240 / 0.01108942080 / -0.03332942080`，completed／flat-equity=`-3.6066180264 / -3.6066807024 bps`、fee cover=`-2.0055150220`、max DD=`0.038119 < 0.50`。第三輪authenticated flat後，remaining session unwind budget`0.066670`不足下一輪完整`0.075` reserve，故entry admission在新下單前按設計block；不得用重啟或重置session accounting繞過。樣本未達30，負值只屬adverse signal，不能冒充正式economic NO-GO。Runtime clean stop，雙authenticated postflight皆position／orders=`0/0`、used collateral`0`、equity=`299.079866`；兩份ignored overlay現為`dry_run:true`、`quote_controller_mode:"shadow"`、`active_unwind_enabled:true`。場後review hardening只取得offline regression proof，沒有重開live，也不改寫上述已執行fingerprint的結果。不得進入Gate D或4h。

VPS 同步與測試仍不在本階段。歷史事故與舊 fingerprint 僅見 [驗證歷史](market_maker_mvp_validation_history.md)，不能代替 fresh preflight。

## 9. Toxicity-aware entry controller rollout

本checkpoint新增bounded external-book feature store、authenticated fill-role attribution、`fixed/shadow/active` controller、entry-only arbiter、controller telemetry與本地offline analyzer。它沒有改Grid production code或shared adapter；OrderManager仍是唯一mutation authority，normal quote仍是`POST_ONLY`。Example YAML繼續保持`dry_run:true`、`active_unwind_enabled:false`、`quote_controller_mode:"fixed"`。

Controller硬邊界：

- 只處理flat inventory下的普通maker entry；non-flat或任何`reduce_only`直接bypass。
- 只能向外widen或移除原本存在的side；不能縮spread、加size、改reference／reservation、改TIF或新增side。
- Economic stop、entry reserve、RiskManager、inventory unwind與uncertain／reconciliation fail-closed均有優先權。
- Active feature未ready、stale、invalid或feature pipeline exception時，flat entry關閉；non-flat exit仍可收斂。
- Markout event同時保存raw BBO與own-size-subtracted external mid；side／authenticated summaries、entry feedback與promotion analysis只接受external markout。External book或managed-order truth不可信時不補值，另以coverage／telemetry error揭露；v1 active仍禁止使用feedback，shadow只供校準。

Protective replacement只適用於同側、non-reduce-only controller entry，且target revision較新、BUY價格更低或SELL價格更高、outward delta達專用threshold並通過專用minimum interval。它可繞過普通market-following reprice threshold，但不能繞過mutation limiter或cancel exact-terminal proof；replacement只能在terminal proof後建立，同revision不得重複cancel。若target在普通minimum lifetime前反轉或向內恢復，立即丟棄pending protective target並安全defer，之後回到既有minimum lifetime、normal threshold與controller hysteresis。任何`reduce_only`退出會清除／bypass pending controller target，不得因entry-controller replacement狀態延遲減倉。

本地分析只讀既有log／metrics，不連線交易所：

```powershell
.\.venv\Scripts\python.exe .\scripts\analyze_market_maker_strategy.py <metrics-or-log> --json-output report.json --markdown-output report.md
```

Shadow輸出是counterfactual proxy，不是queue-fill backtest。Analyzer只把external markout納入策略統計；raw markout僅供diagnostic。若placement後、實際fill前出現shadow block／reprice、controller unavailable或entry不再適用，該fill保守列為`indeterminate`。舊E2ay只有16筆未帶authenticated role join的markout，所以必須維持`pending=16 / indeterminate=16`，不能回溯推斷entry／exit。Periodic checkpoint中的completed episodes依`(session_id, episode_sequence)`合併；無穩定identity的舊schema只讀final snapshot，避免跨checkpoint重複計數。

新schema由`OrderIntentMetadata`明確區分base/controller entry、passive exit與active exit；confirmed live／terminal order ID保留同一intent，partial fill不得改寫，replacement使用新revision，identity或immutable intent衝突須fail closed。Authenticated exit fill必須與同一inventory episode及正整數`episode_sequence`、policy decision、exit stage與binding constraint一致。Executor另以同sequence的policy observation保存無order-ID的active attempt、曾進入inventory hold及最大unlock；沒有該觀察、純telemetry context缺失或schema不完整時，runtime不必因單純telemetry缺失崩潰，但completed episode必須標記`close_policy_coverage:false`、增加missing/incomplete證據並排除promotion／calibration，不得由reason string或舊schema猜補分類。Non-flat期間若authenticated sequence缺失、為`0`／bool／無效或中途漂移，executor必須抑制passive create並維持`passive_wait`／blocked fail-closed，不能沿用舊sequence產生exit intent。

Promotion順序固定：

1. Gate A：`fixed` 30分鐘dry T3，真實create／cancel為0、hard counters全0，並證明既有quote parity。
2. Gate B：`shadow` 30分鐘dry T3，final desired／would-place與fixed一致，feature readiness、decision history及CPU／RSS有界。
3. Gate C：另取明確live授權後才做shadow live；實際仍送fixed quote，至少30 completed maker fills且authenticated natural flat，產出calibration report但不得稱收益證明。
4. Gate D：依authenticated entry markout選較差一側，只開單側widening、不開blocking；base spread、size、mutation、unwind caps與另一側均不變，完整economic/hard-safety gate照舊。
5. Gate E：只有單側active canary完整GO後才測雙側widening；side blocking仍是另一個單變因。
6. Gate F：至少2–3次彼此獨立的fresh-flat short sessions通過後才做4小時；4小時GO後才考慮24小時／VPS。

Config validation只要求active至少啟用一側；第一個active canary的操作政策更嚴格，必須恰好一個`toxicity_apply_*`為`true`，另一側維持`false`。只有該單側candidate完成完整short-live GO後，才可另案評估雙側widening；不得直接以兩側enabled啟動第一輪active canary。

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
- 當時 rollout state：**Gate A GO / Gate B GO / Gate C HARD NO-GO / Gate D–F NOT RUN；recovery完成且account flat**。後續inventory-owner remediation與fresh T3／short-live結果見下節；active controller不因既有dry T3或本次recovery自動promotion。

### Gate C shadow live hard stop（2026-09-02）

- 01:51:12–02:05:12，uptime `845.110s`、`198/198` cycles、failed `0`；真實create／cancel=`27/18`且全成功，maker／taker fills=`9/0`，completed=`8`、round trips=`4`。兩次短暫stale-book pause均撤空managed quote、在下一個authenticated audit checkpoint內恢復；reconciliation failure、unknown、ambiguity／unresolved、mutation block、429、WS reconnect、account read、controller、markout與active counters全為`0`。Shadow history `128/128 complete`，base／applied parity零差異。
- Completed turnover／gross／exact fee／net=`123.670880 / 0.022800 / 0.01484050560 / +0.00795949440`，completed net=`+0.6436029565 bps`、fee cover=`1.5363357971`，max DD=`0.004453 < 0.50`。但只有`8/30` completed fills且第五個episode未flat，正式economic gate未形成。External markout retained／merged／pending=`9/9/0`，9筆shadow proxy全部因後續reprice或exit eligibility保守列為`indeterminate`，不能用來選side。
- 02:05:12 fatal stop原因為`strategy hard stop: soft exit is stranded outside the normal passive quote band by the economic gate`。Shutdown後runtime／orders=`0/0`，兩次authenticated postflight一致為BTC short `0.00020 @ 77207.4`；未做recovery mutation。Raw evidence `logs/market_maker_gate_c_shadow_live_stranded_stop_20260902-020512.log`，SHA `6B96B04ECFA8102773355DC1B372CCFBFCE22D8E7CCF5428DAAE5D929B6D7519`；analyzer history `128/128 complete`。
- 取得明確授權後，02:30:55–02:31:09以未變更的既有helper（SHA `2303F87C...172`）執行單向maker-only recovery；order `844424878317012`為`BUY LIMIT + POST_ONLY + reduce_only 0.00020 @ 76987.8`，exact history為filled `0.00020`、remaining `0`。02:31:26／02:31:36雙authenticated postflight皆position／orders=`0/0`、used collateral=`0`、equity=`299.117762`；runtime=`0`，兩份config仍為`dry_run:true`。Sanitized evidence `logs/market_maker_gate_c_recovery_20260902-0231.txt`，SHA `A22999B200AB1384B9B439DBEB805DC36D11559057EB06974EECE0365E2F1EAC`；recovery不計入Gate C economics。
- 判定：**Gate C hard No-Go；4h未啟動；recovery完成且account flat**。這是設計中的安全停止，不得在live內熱修、放寬economic gate或直接續跑。後續順序為根因修復→offline／fresh dry T3→從Gate C重跑，不能跳過Gate D–F。

### Gate C inventory-owner remediation與授權重跑（2026-09-02）

- **根因與修復**：舊coordinator在`active_unwind_enabled:false`時旁路executor，讓shadow entry雖不改final quote，non-flat卻再次落回stranded fatal。現在executor每cycle擁有flat／non-flat lifecycle；active flag只控制IOC，passive maker exit依authenticated episode／session／drawdown邊界推進。Event telemetry改為單調sequence，analyzer嚴格要求`placement < decision < fill`並排除passive／active exit。Live前稽核另發現partial fill可能留下`0.00010 < min_base 0.00020`；passive與IOC intent皆改送可執行`max(order_size, abs(position))`，經濟投影仍只按實際殘量。沒有修改Grid production或shared Lighter adapter。
- **Offline驗證**：最終inventory executor`21/21`、全部Market Maker`507/507`；full repo`754`只保留既知且同名Grid／Lighter lifecycle baseline`8F+4E`，無新增失敗。`git diff --check`通過。
- **嘗試紀錄 1–3**：03:18首次T3因誤用`--wallet-name main`在connect前拒絕，無runtime／mutation，evidence `logs/market_maker_t3_inventory_owner_shadow_active_20260902-0318.log`，SHA `2A9AD817...F410`。03:19第二次跑滿`347/347`、would-place`439`且mutation／hard counters全0，但PTY中止外層PowerShell，沒有clean-stop證據，故不計完整T3；雙postflight仍為`0/0`，evidence SHA `A9DAB61E...28901`。03:50第三次改用timed stop_event，uptime`1802.687s`、`345/345`、would-place`432`、clean stop與雙postflight`0/0`，evidence `logs/market_maker_t3_inventory_owner_shadow_active_20260902-0350.log`，SHA `F322636D...B1CA`。
- **Short-live attempt 1與回到修復**：04:25啟動後uptime`623.125s`、`141/141`、5 maker／0 taker fills、2 trips，completed net`-0.00456614960`，停止時flat且runtime hard counters全0。但code audit指出sub-min residual P0，因此本次只算diagnostic No-Go；外部Ctrl+C未走clean shutdown並留下可精確歸屬的buy order `844424878138553`，已只取消該單，兩次postflight均position／orders=`0/0`。Evidence `logs/market_maker_gate_c_inventory_owner_shadow_active_short_live_20260902-0426_attempt1.log`，SHA `7354C956...944D`；不得併入後續完整gate。
- **修復後fresh T3**：04:40–05:10，uptime`1802.547s`、`345/345`、failed`0`、would-place`443`；真實create／cancel、reconciliation failure、unknown、ambiguity／unresolved、429、WS、account read、active與episode-cap counters全0。Controller history`32/32 complete`，clean unsubscribe／disconnect／stop，雙postflight`0/0`。Evidence `logs/market_maker_t3_inventory_owner_shadow_active_residual_fix_20260902-0440.log`，SHA `1E6C0B3E...01B1`。
- **完整30分鐘short-live attempt 2**：05:13–05:43，timer exit、uptime`1806.282s`、`369/369`、failed`0`；create／cancel=`16/10`且全成功，6 maker／0 taker fills、3 round trips，三個episode均`maker_flat`，active attempts／blocks／ambiguity全0。Reconciliation`369/0`，unknown、unresolved、429、WS reconnect、account read、mutation limiter、controller與log errors全0；10次cancel warning全由exact-terminal reconciliation收斂。Controller history`62/62 complete`；執行時analyzer分母只有3筆authenticated non-active entry，全部classifiable／likely-filtered，3筆passive exit正確排除。第三輪flat後entry reserve按設計block，沒有為湊樣本重啟。Evidence `logs/market_maker_gate_c_inventory_owner_shadow_active_short_live_20260902-0513_attempt2.log`，SHA `64C04E96...4403`；當時analyzer JSON／Markdown SHA分別為`1E268254...A349`／`A6D3322E...F774`；runtime0、雙postflight`0/0`，費率仍為premium maker／taker `0.00012 / 0.00035`。
- **場後review hardening**：獨立review重現跨多log／restart時單調sequence可能互相污染，故每個runtime新增不可重用run ID，decision／placement／fill及history completeness只在同run內比較；analyzer再以純state fold輸出`would-place／reprice／block／cancel／resume`、fill當下virtual-live與價格，且維持`true_queue_backtest:false`。第一版fold曾略過「有sequence但缺run ID」的foreign／legacy decision，最終review以cross-input case攔截並修為立即`event_sequence_incomplete`；只有帶有效且明確不同run ID的decision才可略過。另為long surplus boundary的`1 - maker_fee - minimum_rate <= 0`加入fail-closed guard，非正分母不建立passive／active order。這些變更不改正常Gate C quote／mutation路徑；只做offline regression，未重新live。舊attempt2 log產生於run-ID schema之前，因此用最終analyzer重播會把3筆entry保守列為`event_sequence_incomplete`／indeterminate，3筆passive exit仍排除；post-hardening JSON／Markdown SHA=`6A0DF742...8957`／`7AA20EF1...12F`，不得以舊的3/3分類當作新schema可重現證據。
- **本輪結論**：要求的完整`T3 → 30m short-live`已完成，已執行fingerprint的inventory ownership與hard-safety為GO；但只有`6/30` completed fills，正式economics仍`collecting`。場後hardening只有offline proof；不得升Gate D、不得以負樣本提前宣告正式economic NO-GO，也不得進行long-live。本輪明確未啟動4h。

### Post-27abae Phase 0–2 checkpoint（2026-09-02，code-only）

本checkpoint完成sanitized golden fixture、typed order intent／authenticated exit-policy attribution，以及outward protective controller replacement。Golden fixture來自既有Gate C attempt2 log；artifact `run_id`不是runtime event-sequence ID，原始runtime未嵌入commit/config hash，因此兩者維持`null`，`27abae1845a1d3d4542d0de480518e60eb0d8829`只記為post-run baseline，不能冒充executed fingerprint。Fixture固定為`diagnostic_only`、active-controller proof `indeterminate`、`promotion_eligible:false`，不得補造新schema coverage。

Typed evidence現在以order identity及authenticated episode sequence綁定，另保存episode policy observation，讓HOLD期間舊單在cancel race中成交、未取得order ID的active attempt、以及同單未replacement時的unlock軌跡仍可保守記錄；缺少完整producer evidence一律不可promotion。Controller block/resume只接受arbiter明確標記的side，不能把inventory/risk/economic-stop撤單誤算成toxicity block。Stage transition使用獨立session-monotonic sequence，直接authenticated-flat完成也不會與前一policy decision去重碰撞。

本checkpoint的最終offline驗證數字記於驗證歷史E2bb。本輪未連線交易所、未做account mutation、T3、live、flatten或long-run，亦不改判先前Gate C live結果。下一步仍須fresh fixed T3 → shadow T3 → active dry T3；active live與Gate D promotion尚未取得證據。

### Dry virtual-order lifecycle checkpoint（2026-09-03，code-only）

E2bb場後稽核確認，舊dry-run沒有保存`would_place`後的order slot，因此即使active controller產生新revision，也無法以runtime證據證明「既有quote被protective cancel、terminal後下一cycle才reprice」。本checkpoint只補這個驗證邊界，不改live adapter mutation路徑：dry-run建立帶唯一synthetic ID且`simulated:true`的虛擬slot；`would_cancel`清除slot並強制replacement延到下一cycle；每cycle最多一筆create，獨立dry mutation window及risk-reducing emergency budget與live決策一致。Authenticated open-order sync仍照常讀取交易所，零真單時保留虛擬slot，遇任何真實order立即fail closed；synthetic ID不登記為known／authenticated order，且虛擬quote不從external book扣量。Status另外區分typed protective／block／resume would-actions、dry limiter defer及simulated quote context。

Deterministic evidence另補兩個Phase 1／2明確鏡像：drawdown boundary較episode／session boundary更緊時必須輸出`DRAWDOWN_CAP`及對應budget；同一exchange order ID若跨authenticated inventory episode被重用，必須在不覆寫既有binding、ledger或seen-trade狀態下atomic fail closed。首版受影響模組`357/357`、全部Market Maker`561/561`；full repo`808`維持既知且同名Grid／Lighter baseline`8F+4E`，無新增失敗；py_compile及`git diff --check`通過。兩次獨立read-only review都判定無P0／P1 blocker。

Fixed T3 attempt 1執行的不是新commit：base HEAD為`fe18432130d93da662c3a0597a83be55dd7739af`，runner沿用該HEAD；以HEAD字串及下列5個當時runtime override SHA-256逐行合併所得worktree core fingerprint為`7932A3C219FDF36E5ED179636FF018B6A40C504AC79FE6F62F85D5D7CBEA7F2B`：

- `coordinator.py`：`C6B3EEF0A13C017F4A8235A3883242A6E439DE206FE32B9E7E8FA6EE7E1522DA`
- `market_features.py`：`238510A27AAE3A6D8694AC9908DC29C335187C18A2F7BCC5377F6A2185F16AB4`
- `metrics.py`：`89AA19F6A9FBB54E67604C681850D773775F53380AFF70492AC8E67BA37C9D5F`
- `models.py`：`BF06B41023988FD5D99F901C7F36BDC570C52E5C1DBEC3A12BFF0212A3E59216`
- `order_manager.py`：`53611EC84FEB8952860D11E82A2CE6702E07FA9226F246A9FDD5E40CA33EA01D`

本checkpoint沒有執行live、flatten或account mutation，也沒有取得active live授權。下一步固定為fresh authenticated preflight後依序執行fixed、shadow、active三組30分鐘dry T3；任何一組若失敗，先保存attempt ledger並回到修復／offline regression，不得跳往live。

#### Fixed T3 attempt 1與觀測性修復（2026-09-03）

00:20:21啟動fixed dry T3；最後可信status為uptime`584.796s`、`111/111` cycles、failed`0`、position`0`、真實create／cancel`0/0`、would-place／cancel`20/19`，reconciliation failure、unknown、ambiguity／unresolved、mutation limiter、429、WS及controller error全`0`。但每10秒status重複輸出整份controller history／quote context，`market_maker.log`在584秒膨脹至`48,533,608` bytes，外層PTY因此消失，沒有clean-stop與final marker，故本attempt依證據完整性判為**harness／observability NO-GO**，不是策略或帳戶事故。Evidence `logs/market_maker_t3_fixed_virtual_attempt1_20260903-003002.log`，SHA-256 `76655D295BBD758C959648B2EF0D31BAD748D958D26BA8522E934379CE41200E`；00:32:29與00:32:42兩次authenticated postflight皆signer／wallet PASS、position／orders=`0/0`、used collateral=`0`，無交易所mutation。

最小修復第一階段只改status evidence transport：週期status對controller decisions、quote contexts及fill markouts只輸出新增／實質變更的record並附retained count，cache上限沿用runtime retention；dry-run clean stop後由CLI best-effort輸出`market_maker_final_dry_run`。Final marker缺失即使exit 0仍是T3 NO-GO，分析器必須讀整份保存log，不得把incremental final line當獨立完整快照。Live cleanup與mutation路徑不變。

00:46:29重跑的attempt 2仍因PTY transport在最後可信uptime`474.766s`後消失；`90/90` cycles、failed`0`、position`0`、真實mutation及hard counters全`0`，但沒有final marker／clean-stop，故仍為**harness NO-GO**。File端只有`661,049` bytes／48 lines，證明不是line-cap或策略問題；每個週期status仍約13KB，經PTY換行後每45秒產生數萬tokens。Evidence `logs/market_maker_t3_fixed_virtual_attempt2_20260903-005419.log`，SHA-256 `DDA92CB3D1DDB4C84B9787466C6CC65AF89D5AFDA171AC34236480132E00E390`；00:55:19與00:55:34雙authenticated postflight皆position／orders=`0/0`、used collateral=`0`。Analyzer讀取完整attempt 2 log得到47 snapshots、controller history`85/85`且complete，確認incremental evidence contract有效。

第二階段保留file payload不變，只在`market_maker` console handler過濾`Market maker status:`，因此完整週期與final snapshot仍進入log，其他lifecycle／warning／error與clean-stop照常顯示；成功final snapshot後另輸出短標記`market_maker_final_dry_run emitted`。精準filter／incremental／final tests`3/3`、全部Market Maker`564/564`；full repo`811`仍只保留既知且同名baseline`8F+4E`，py_compile與diff-check通過。獨立review未發現30分鐘dry T3的P0／P1；但現有`LineLimitedFileHandler(max_lines=1000)`不足保留未來4小時的incremental status，因此在修復跨segment保存前，4h evidence/live仍為明確blocker。

01:00:36啟動attempt 3後，執行路徑review確認Windows直接Ctrl+C雖會執行coordinator cleanup，`asyncio.run` cancellation仍會跳過cleanup後的final-marker區段；因此在92.188秒主動提前停止，避免把不可取得final marker的路徑跑滿。最後為`18/18`、failed`0`、position`0`、真實mutation與hard counters全`0`，unsubscribe／disconnect完成但只有`Market maker stopped after operator interrupt`，依gate仍為**execution-path NO-GO**。Evidence `logs/market_maker_t3_fixed_virtual_attempt3_ctrlc_path_nogo_20260903-010210.log`，SHA-256 `DDC930567B2A7D7DFE696EAEF45B252A2ED14EAEFF92EE2570F43DBA32FB4FF6`；01:03:31與01:03:46雙authenticated postflight均position／orders=`0/0`、used collateral=`0`。後續30分鐘T3固定使用in-process timed `stop_event`正常返回，不再依賴Ctrl+C。

01:04:32–01:34:29的attempt 4首次完整證明timed stop、full final record及clean stop；final `344/344`、failed`0`、position`0`、真實create／cancel`0/0`、would-place／cancel`71/70`，全部hard counters`0`。Analyzer讀取180 snapshots，controller history`333/333 complete`、fixed base／shadow／applied parity成立，71個quote context全為`simulated:true`；runtime0且01:35:15／01:35:30雙postflight position／orders=`0/0`。但final uptime為`1799.687s`，嚴格低於`>=1800s`門檻`0.313s`，因此只判**duration-only NO-GO**，不得前進shadow。Evidence `logs/market_maker_t3_fixed_virtual_attempt4_final_20260903-013429.log`，SHA-256 `7AFD52B13756EA0704D91D5A8D020CD6816B2F3C90788E5B56C60AF2CC11FA5B`；analyzer JSON／Markdown SHA分別為`B7DADB2997FB5380A135DFFEDCA22B8A0E2F95C6FBA9C6FF84F8AB34CD1D6749`／`B81A05CBFF9B608EEEC61A1EBD05F6AB7A99DA6566C23BF776AEFDE3826DE6A5`。下一次timer設`1810s`吸收排程邊界，仍要求final uptime至少1800秒。

01:36:36–02:06:44的attempt 5以同一fingerprint與config、`1810s` timed stop完成正式fixed T3：final uptime`1810.109s`、`347/347`、failed／consecutive`0/0`、position`0`、真實create／cancel`0/0`、would-place／cancel`62/60`，reconciliation`347/0`且全部hard counters`0`。181 snapshots由analyzer合併controller history`332/332 complete`；fixed base／shadow／applied parity成立、62個quote contexts全`simulated:true`、external reference未含own quote。Full final marker、clean stop、runtime0與02:07:22／02:07:37雙authenticated postflight position／orders=`0/0`均成立，故**fixed dry T3 GO**，可前進shadow dry T3。Evidence `logs/market_maker_t3_fixed_virtual_attempt5_final_20260903-020644.log`，SHA-256 `F85FB81078A59984D36BB07918318F3F8AD269E13CA0CC9B079606C44F4ECBBA`；analyzer JSON／Markdown SHA為`FB56749C0F7B37723D0F2AAFF0D44EABA55C5C8F15AE27212188E4B111FF967D`／`817A08D8B51160EFA50F5071A25317EDB6533EDE0E9F72E9ADD16C3810EB8648`。

第二階段修復後重跑候選仍以base HEAD `fe18432130d93da662c3a0597a83be55dd7739af`為基礎；加入`run_market_maker.py`及5個Market Maker runtime檔的SHA逐行合併後，worktree runtime fingerprint為`DAC24AFE2A4B61310F269361CE39CFC5D2AD4ED67454B65962B84E7EDEEB9D5B`。Attempt 2的第一階段候選為`B441219E400963385ACC57F62DC032F7492DD9EAA9B9C0FBE2787D3AE6939068`；attempt 1的`7932A3C219FDF36E5ED179636FF018B6A40C504AC79FE6F62F85D5D7CBEA7F2B`則代表base runner加5個當時runtime override，三者不得混用。

#### Shadow dry T3 attempt 1（2026-09-03）

02:10:02–02:40:13以相同runtime fingerprint、shadow config SHA-256 `9571AFF52C864D1F701C70F44A676DCFDD972C25013AB00BDCE1E0570290308E`及`1810s` timed stop執行。Final uptime `1810.110s`、`345/345` cycles、failed／consecutive `0/0`、position `0`、真實create／cancel `0/0`、would-place／cancel `65/64`、reconciliation `345/0`；ambiguity／unresolved／unknown／actual mutation block／429／WS／account／controller與markout hard counters全為`0`。181 snapshots只有一個run ID；analyzer合併controller history `10/10 complete`、feature health `ready/warming=380/19`，controller ready／warming時間分別為`1702.797s / 99.453s`。65個雙側quote contexts全為`simulated:true`，raw status、history與contexts的base／applied逐筆零差異，並觀察到15個有效shadow counterfactual signals；external reference從未包含虛擬own quote。Full final marker、clean stop、runtime0及02:40:33／02:40:46雙authenticated postflight position／orders=`0/0`、used collateral`0`均成立，故**shadow dry T3 GO**，可前進active dry T3。

首次raw validator曾把`would_mutation_limiter_deferred=1`與actual hard counters混為一類而誤報NO-GO；這是驗收allowlist問題，不是runtime失敗。事件發生於uptime`244.360s`：8/min dry mutation window收到第9個一般reprice request，typed protective defer及actual `mutation_limiter_blocks`仍為`0`，兩側simulated slot保持live，且`30.000s`後已有下一筆virtual lifecycle進展，之後counter不再增加。Dry `would_*`與actual/hard counters必須分池：`would_mutation_limiter_deferred>0`觸發boundedness review，不自動NO-GO；只有伴隨actual block／error／slot破壞／non-flat或protective urgency，或超過一個60秒window仍無進展，才判rollout NO-GO。不得為消除此安全節流訊號提高mutation cap。

Evidence `logs/market_maker_t3_shadow_virtual_attempt1_final_20260903-024013.log`，SHA-256 `C2CD4A67E42804745E102A5D8623CC7FA4375ED2F321BCE392796CDC2D8C5A7E`；analyzer JSON／Markdown SHA分別為`47C13B72C1034B95D45E2F830610F80DCDBAF6C1406C6BCED2F17A1EF5A3CBD1`／`950197FB3B8511626CD7EC71C8F26935E03DACC644A2B1162332CC0E0E7CB977`。本場只有dry authenticated reads，沒有exchange mutation、fill、flatten或live授權。

#### Active dry T3 attempt 1：安全GO、lifecycle evidence incomplete（2026-09-03）

02:50:30–03:20:42以active engineering-only config SHA-256 `3173534BC2286650506BCBE5E8E92B074007E706AC252BCFC3AB96472C2E7EC9`及`1810s` timed stop執行。Final uptime `1810.156s`、`346/346` cycles、failed／consecutive `0/0`、position `0`、真實create／cancel `0/0`、would-place／cancel `52/51`、reconciliation `346/0`；全部actual hard counters與dry limiter defer均為`0`。單一active segment有181 snapshots，controller history `304/304 complete`（final retained cap `200`）、52個雙側contexts全為`simulated:true`；所有ready且entry-applicable decisions的bid applied均不在base內側、ask applied均等於base，external reference未含虛擬own quote。Final marker、clean stop、runtime0及03:21:23／03:22:01雙authenticated postflight position／orders=`0/0`、used collateral`0`成立。

但明列required gate `controller_protective_would_cancel / controller_protective_would_reprice`最終為`0/0`，沒有自然證明cancel terminal後不同cycle replacement，因此本attempt只能判**safety GO／active lifecycle evidence-incomplete NO-GO**，不得前進live。根因不是code exception，而是engineering overlay校準：303個ready history的bid extra ticks分布為`0:4 / 1:2 / 2:1 / 3:296`，bid toxicity score min／median／mean／max為`0 / 50.483 / 56.486 / 268.608` ticks；既有`widen_start=1`令active bid幾乎全場一ready就飽和3 ticks，極少形成「既有較窄quote→向外revision」。下一步只校準ignored、dry-only active overlay，不改fixed／shadow、runtime algorithm或production config；focused regression通過後必須完整重跑active dry T3，offline invariant不能替代runtime evidence。

本次file handler採append，`market_maker.log`同時含前一個shadow run；combined raw已保留，並由明確`Market maker started`時間及active run ID切出184行單一segment。Combined evidence `logs/market_maker_t3_active_virtual_attempt1_combined_20260903-032042.log`，SHA-256 `9946D8B25C2ABD4BC963FA79D44EB149C703777968AAF2F7AA663A1E5A762FAA`；active segment `logs/market_maker_t3_active_virtual_attempt1_evidence_incomplete_20260903-032042.log`，SHA-256 `ACD3E121740DB4F91988A79F799123E67ED3BD1862CC3FF2CB7D9F81600451B6`；analyzer JSON／Markdown SHA為`9628D6A0906F4A088DFC287D78B845F6B83D8B8B275F0AE093B2E0C2601B4C64`／`E1C8DB7C7CB3300F49FE605C8E2E2258DD0090CDEF881CA5BDB16258FB56740C`。後續重跑前先輪替／驗證live log為空，避免再次產生混合raw；本場沒有live、fill、flatten或exchange mutation。

#### Active dry T3 attempt 2：校準後lifecycle GO（2026-09-03）

Attempt 1的303筆ready／applicable decision以不同門檻離線重映射後，`widen_start=50`預測extra ticks分布為`0:138 / 1:9 / 2:3 / 3:153`，另有37次upward transition及34次`0→positive`；因此只把ignored、engineering-only active-dry overlay的`toxicity_widen_start_ticks`由`1`調為`50`，保留bid-only、`max_extra=3`、`block=0`、feedback OFF，不改runtime formula、fixed／shadow config或production default。Overlay載入實測為`dry_run:true / active / apply_bid:true / apply_ask:false`，SHA-256 `126DCDE098AE01F74E0CE1DAE372197598230469C44CC1F1A4ABE53F3E10A8C6`；focused config／controller／arbiter／order-lifecycle及dry protective limiter regression為`38/38`通過。因runtime fingerprint未變且fixed／shadow使用各自config，沒有重跑已GO的兩場。

03:27:44 fresh authenticated preflight為runtime／position／orders／used collateral=`0/0/0/0`；先把既有combined log移至`logs/market_maker_pre_active_attempt2_combined_20260903-032722.log`並驗證SHA仍為`9946D8B25C2ABD4BC963FA79D44EB149C703777968AAF2F7AA663A1E5A762FAA`，再以空白live log及`1810s` in-process timed stop執行。03:28:11–03:58:19 final uptime `1810.125s`、`346/346` cycles、failed／consecutive `0/0`、position `0`、真實create／cancel `0/0`、would-place／cancel `63/62`、reconciliation `346/0`；全部actual hard counters、controller／policy error、unknown／ambiguity、429、WS與account-read failure均為`0`。181 snapshots只有run ID `56d0a71c10334bca9f63f8d6c4c4ab4f`；controller history `322/322 complete`（321 ready，ready後無退回unready，final retained `200`），63個contexts為buy／sell `36/27`，全為`active + simulated:true + reduce_only:false`。Ready且有bid價格的extra分布為`0:82 / 1:1 / 2:5 / 3:129`；bid applied從未在base內側，ask applied逐筆等於base，external reference從未包含虛擬own quote。

Required typed lifecycle取得`controller_protective_would_cancel / would_reprice / would_defer = 3/2/0`。至少兩組自然序列有完整order identity與event順序：(1) BUY `dry-run-10 @ 77108.0` placement event 64，decision 59／event 69向外觸發cancel，後續decision 60／placement event 71以新ID `dry-run-11 @ 77092.6`重掛；(2) BUY `dry-run-52 @ 77216.0`於event 331取消，後續decision 280／event 332、placement event 333以`dry-run-53 @ 77185.8`重掛。兩組都跨不同cycle／event、使用不同synthetic ID且BUY價格只向外，故**active dry lifecycle GO**。

本場另完整保留兩類非hard telemetry。`controller_protective_reprice_deferred=4`全部來自第二組protective cancel後target在普通minimum lifetime內反轉，依§227丟棄pending target並回normal hysteresis；counter在uptime約1012–1032秒累積後停止，約1042秒已建立普通BUY，40秒內恢復，之後第三組protective仍能完成。`would_mutation_limiter_deferred=3`則是一般dry scheduler，三筆分別約13、4.3及8.3秒內有後續virtual進展，皆小於§339的一個60秒window；protective would-defer、actual mutation block、error、non-flat與slot破壞皆為`0`。兩者均經boundedness review通過，不得抹除，也不得誤歸為actual hard failure。

Raw evidence `logs/market_maker_t3_active_virtual_attempt2_final_20260903-035819.log`為184行／3,146,072 bytes，SHA-256 `FEE4CC1C974A66D4B8AC9439449754156EE643A6C740A41BEDE4E80BA34609E9`；analyzer JSON／Markdown SHA為`3EEDABFC7D4D4A1798070E1695A4D36720C703EE64110A8FD40A4F812B48D793`／`FD447DA03467075F644A69D4B1E13E581A963D1D6CCE1D4AB15B03105B6C6471`。Final marker、clean stop、runtime0成立；03:58:41及03:59:14兩次authenticated postflight皆position／orders=`0/0`、used collateral`0`、equity`299.079866`。本checkpoint至此為**fixed dry GO／shadow dry GO／active dry GO**；它不等同live授權或economic／production promotion，本場沒有live、fill、flatten或exchange mutation，也未啟動short-live或long-live。

### Phase 3 local-read-only calibration campaign（2026-09-03，code-only）

本地入口為`.\.venv\Scripts\python.exe .\scripts\mm_calibration_campaign.py <manifest.json>`。它只讀manifest指定的本機證據並把JSON寫到stdout；`analysis_contract`固定為`local_read_only:true / network_access:false / exchange_mutation:false / live_profile_generated_or_applied:false`。不得由本工具建立或套用profile、連線exchange、讀寫帳戶、啟動runtime、flatten、送出mutation或自動前進active／live。

Manifest固定campaign／candidate、expected commit SHA、config SHA、network、expected account identity SHA、controller profile、symbol、maker／taker fee及操作者的`max_cumulative_flat_loss_usdg`；每個snapshot也必須帶相同campaign／candidate與完全匹配的commit／config／network／account／profile／symbol／fee identity、單一且跨input不重用的runtime run ID，以及有效正區間UTC start／end。工具逐input輸出SHA-256並把ordered input roster納入`campaign_evidence_sha256`；原始證據必須immutable，`manifest.inputs`必須以前一checkpoint為ordered prefix、只能append，且每版manifest、report、`source_sha256`與campaign digest都須保存比對。不得移除、替換、重排失敗／虧損／diagnostic場或更換campaign ID來重置紀錄。分析器能偵測單次manifest內的重複／混用，無法跨invocation自行發現被省略或改序的檔案。

Final evidence另要求authenticated preflight與postflight皆position／open orders=`0/0`、authenticated account audit、signed／audited／ledger position皆flat、authenticated open orders=`0`，並完整提供且歸零`ambiguous_submissions`、`ambiguous_cancellations`、`unresolved_cancellations`、`reconciliation_failure`、`unknown_orders`、`mutation_limiter_blocks`、`http_429`、`active_unwind_ambiguous`、`markout_telemetry_errors`及`failed_cycles`、`consecutive_errors`、`controller_error_count`。Controller history必須reported total為正、merged等於total且`complete:true`；eligible seconds、maker fills／turnover、completed net、flat-equity change、drawdown亦須完整有效。`fill_markout_coverage.unit`必須為`observed_order_fill_delta`，新增的累計`observed_event_total`必須恰等於campaign從完整輸入合併出的event數，避免rolling buffer截斷後仍誤判coverage完整。

校準分母只接受`observed_order_fill_delta`中的authenticated ordinary `entry`；每個maker event必須和account audit內的typed intent、role-compatible attribution、stable trade／order signature、root run ID、event sequence及同run的placement quote context閉合，且每筆entry恰好對應一個同side、完整的policy episode。Passive／active exit attribution另須具有效policy decision、canonical exit stage與binding constraint；active taker attribution數必須等於`unique_taker_fills`。BUY／SELL分側統計，`risk_increasing`只保留作attribution、`passive_exit`與`active_exit`分開排除，其他pending／indeterminate不補值。每個side × toxicity score bin除entry count、5s／15s markout、負值機率及episode／inventory／exit統計外，另輸出maker fills、maker turnover與completed episode net的每eligible-hour值；三者都以campaign eligible quote hours為共同分母，fills計歸屬該bin完整episode內的unique authenticated maker trades，turnover計同episode內所有authenticated maker fill-delta notional，net則計可守恆且去重後的completed episodes。三個bin numerator必須各自exact守恆到campaign totals，不能把entry fill-delta數冒充maker fills。每筆entry都必須有placement toxicity score及external 5s／15s markout。完成條件是BUY與SELL各至少30筆authenticated entry、每側至少2個score bin且每bin至少5筆、至少2個彼此不重疊的fresh UTC run、所有markout／score／episode coverage完整、沒有任何diagnostic input，且campaign risk evidence完整、累計realized flat losses後的操作者budget仍嚴格大於0。Manifest roster內凡具可信authenticated final-flat財務證明者都會保守扣入風險，即使fingerprint／identity不符而只能列diagnostic；duplicate run ID取較差loss一次，無run ID證據另列unidentified。不能靠破壞identity或排除壞場讓風險回補；任一條件缺失時`campaign.status=calibration_incomplete`、列出`incomplete_reasons`且`recommendation_allowed:false`。Counterfactual仍只是proxy，不是queue-fill backtest。

Authority parser拒絕duplicate key、NaN／Infinity、過深nesting、非object與超界Decimal；所有per-input schema／arithmetic錯誤只可降為diagnostic，且strict parse後會先抽取可信final-flat loss再呼叫strategy analyzer，避免壞的非財務欄位把虧損移出campaign risk。Cumulative hard counters逐snapshot為0，UTC identity、raw fill event的immutable欄位與非null canonical 5s／15s markout、同order placement context皆不得漂移。Attribution position chain必須從flat開始、只由同一entry order建立、途中不得重回flat、最後完整flat；observed maker fill amount、episode quantity、entry／exit position delta、final exit policy、completed episode ledger與account gross／fee／net／count必須exact守恆。Controller history total必須為exact integer；非fill decision需同run sequence／decision／bid／ask schema及有效score（或明確warming／inapplicable），任何controller error都屬diagnostic；`maker_fill` history另須綁定實際fill order、placement context、fill-observation sequence及前一controller decision。帳戶baseline／current／flat change、gross－exact fee＝net與drawdown identity也須exact成立。

缺run identity／executed metadata、pre/postflight、flat或hard-counter proof、nonzero complete history、完整economics／coverage，或存在duplicate run、episode identity conflict、不可讀／mixed input的場次，一律只進`runs.diagnostic_only`；authority input僅接受完整JSON object／object array或每一非空行皆為object的strict JSONL，任何malformed或非object record都使整個input diagnostic，不得回退較舊snapshot。任何diagnostic都令整個campaign保持incomplete。E2bg–E2bj的fixed／shadow／active dry T3仍維持其歷史runtime／lifecycle GO，但本checkpoint新增`observed_event_total` telemetry schema，故它們不驗證目前新fingerprint；既有raw log也沒有上述decorated envelope與real authenticated entry sample，只能作diagnostic，不能回填或啟動校準。此checkpoint只建立offline pipeline，**沒有執行任何real campaign或新runtime**。本checkpoint當時runner尚未原生輸出完整decorated envelope；該缺口已由下一節的runner-native producer補上。仍須以clean committed fingerprint完成fresh T3，之後才可另取逐場live授權與fresh-flat shadow evidence。不得建立active profile、直接啟動active／4h live或重用舊log冒充coverage。

本checkpoint驗證：campaign targeted `51/51`、全部Market Maker `615/615`；完整repository `862` tests維持既知且同名的Grid／Lighter baseline `8 failures + 4 errors`，沒有新增Market Maker failure。`py_compile`與`git diff --check`通過。

### Phase 3 runner-native calibration evidence envelope（2026-09-03，code-only）

Runner現在可以選擇性直接產生campaign authority input；三個參數必須同時提供：

```powershell
.\.venv\Scripts\python.exe .\run_market_maker.py <config.yaml> --dry-run --wallet-name <profile> --evidence-output logs\market_maker_evidence\<unique-run>.json --campaign-id <campaign-id> --candidate-id <candidate-id>
```

`--evidence-output`只允許新的、Git-ignored且位於`logs/market_maker_evidence/`下的local JSON path，不覆寫舊檔。Runner會在讀取wallet profile前驗證目前HEAD為40位commit SHA且整個worktree（含untracked）乾淨，並在結束時重驗一次。Semantic config SHA由CLI `--dry-run`覆寫後的實際`MarketMakerConfig`全欄位、型別與canonical Decimal決定；所有snapshot會凍結相同campaign／candidate／commit／config／network／account identity／profile／symbol／fee與UTC開始結束身分。因此權威T3不得從未commit或中途改動的worktree產生。

`network`是明文且正規化的`robinhood`或`robinhood_testnet`。`account_identity_sha256`是以domain-separated canonical JSON `{schema: lighter_account_v1, exchange: lighter, network, account_index}`計算的穩定pseudonym；不含private key、API key index或raw wallet address，所以credential rotation不會改變它。Manifest必須把artifact內的值分別填入`network`與`expected_account_identity_sha256`；任何snapshot缺值或跨network／account混用都只可進diagnostic。此SHA用於一致性，不是credential、owner proof或對低entropy公開account index的保密承諾。

Evidence mode在dry-run也會強制啟用authenticated read-only account monitor，即使`account_audit_interval_seconds: 0`也一樣；不會因此發送create／cancel或其他exchange mutation。Preflight必須在第一個quote cycle前完成，postflight必須在order-manager shutdown與最後audit後、disconnect前完成。邊界不再拼接兩次獨立REST read；它使用同一次timeout-bounded audit中前後確認的trade IDs與open-order IDs，要求audited position／open orders為`0/0`，並另外要求最新WebSocket position cache為flat。舊REST position不會回寫覆蓋較新的WebSocket non-flat狀態。

Live evidence另外fail closed要求`require_flat_start: true`、`startup_open_order_policy: abort`、`exclusive_symbol_control: true`與`cancel_on_shutdown: true`，並在adapter factory／connect前拒絕不符設定。Caller-supplied stop event有獨立watcher；若預先已設定或在connect／subscription／audit期間到期，coordinator會在可用的startup checkpoint停止，不得穿透到第一筆live mutation。Dry final marker只在startup確實完成、cleanup成功且final state為`STOPPED`時輸出；Windows cancellation若完成同樣的cleanup，仍允許輸出含postflight的final marker後重拋e取消。

Writer只接受strict finite JSON-safe狀態，拒絕sensitive key、已保護credential value、non-finite數值、過深或未知型別。最後先寫private sibling temporary file、flush／`fsync`，再以exclusive hard-link發布；同名檔存在或寫入失敗時不會破壞舊證據。若runner、identity、git、terminal event、authenticated audit或flat boundary不完整，輸出會將`commit_sha` 設為`null`、附上`evidence_integrity_errors`並以非零狀態結束；這類檔只是diagnostic，不得納入campaign denominator。

本checkpoint只完成producer接線與offline／fake-runtime驗證；network／account混用已由manifest、逐snapshot identity與campaign digest三層fail closed。Campaign targeted `54/54`、全部Market Maker `649/649`通過，full repository `896` tests只保留同名baseline `8 failures + 4 errors`，`py_compile`與`git diff --check`通過。本輪沒有啟動真實runtime、沒有連線或account mutation，也沒有產生current-fingerprint T3 authority artifact：目前worktree尚未commit，而這正是producer必須拒絕的狀態。下一步是取得明確commit／push授權後固定fingerprint，再執行fresh 30分鐘dry T3；不得因本次code-only GO啟動shadow live、active live或4h。
