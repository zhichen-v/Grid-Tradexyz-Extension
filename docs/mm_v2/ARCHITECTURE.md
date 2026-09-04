# Market Maker V2 — Architecture contract

> 設計契約，逐階段實作。權威來源：[rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)。產品目標／fee floor／授權／complexity budget 見 [OBJECTIVE](OBJECTIVE.md)。Phase/run 驗收只記在 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

Phase 2–5 的 immutable models、五個 ports、ledger、quote policy、governor 與 bounded execution 已接入 Phase 6 `VolumeSession`／獨立 runner。`dry_synthetic_cycle`／`DrySafetyExecutionPort` 保留空計畫 smoke 契約；`DryVolumeExecutionPort` 支援非空報價意圖但不虛構成交。依 2026-09-05 使用者明確指示，Market Maker 僅保留 V2，不再載入或保存 V1 package；必要 execution safety 由 V2 自有模組承接。實際 rollout 證據以 EXPERIMENT_LOG 為準，移除 V1 本身不是 live GO。

`LighterAccountPort` 接入公開 authenticated account、實際 fills 與 current fee／funding，不含舊 episode 政策。共用 Lighter adapter 的 fee／funding／USDG metadata 與 opt-in read stream 都沿用既有 signer；V2 不讀 private signer／REST、不另建 signer、不用歷史成交費率代替 current fee。只有明確開啟 read stream 才將公開 open-orders 讀取切換至 fresh authenticated WS snapshot；Grid 不開啟此路徑，原 REST 行為保留。

`VolumeExecutionPort`／`BoundedExecutionPort` 使用 V2 自有 [order_manager.py](../../core/services/market_maker_v2/order_manager.py) 的 `MarketMakerOrderManager` 與 [execution_models.py](../../core/services/market_maker_v2/execution_models.py)。`config.py` 的 `ExecutionSettings`／`execution_settings` 只提供必要的 immutable execution 欄位。已驗證的 mutation／ownership／cancel／IOC terminal 語義保留，controller／toxicity／intent-attribution 舊政策支援移除；策略不得直接操縱 manager private slots。

## 隔離與 ownership

V2 使用獨立的 `run_volume_market_maker.py`、`core/services/market_maker_v2/`、`config/market_maker_v2/` 與 `test_mm_v2_*.py`。不保留 V1 runtime／config／tests／docs 或相容入口，不引入舊 episode、toxicity、reason-string 或 private-slot 依賴。必要 execution safety 由 V2 的 `test_mm_v2_order_manager.py`／`test_mm_v2_lighter_execution.py` 及既有 public-contract tests 驗證。Grid production 不在本次範圍。

| 元件 | 唯一責任 |
|---|---|
| MarketState / MarketDataPort | 去除可識別 own volume 的 external BBO、有效 microprice（否則 mid）、有限長度 EWMA；拒絕 stale/untrusted 資料 |
| QuotePolicy | 由市場、inventory 與 governor constraints 產生每側至多一張正常 POST_ONLY 意圖；沒有交易副作用 |
| InventoryGovernor | soft/hard bands、dynamic pretrade risk reserve、hold/loss/deadline/stop 觸發、flatten/cooldown 決策 |
| ExecutionPort / AccountPort | V2 自有執行模組與 authenticated truth 的窄介面；單一 mutation authority、exact cancel/IOC terminal proof、unknown/uncertain fail closed |
| SessionLedger | 去重 fills、partial aggregation、全 session economics、time-weighted inventory、最終平倉與 fee 歸因 |
| Orchestrator | 取得快照 → governor constraints → quote/execution → ledger/telemetry；不承擔策略、帳本或交易細節 |
| Clock / TelemetrySink | 可測試的時間及 append-only JSONL 事件；不新增 campaign validator |

## 報價與 inventory

Reservation price 的 bps shift 為 `-clamp(position / hard_limit, -1, 1) * skew_bps_at_hard`。Long 向下偏移、short 向上偏移；bid 向下取 tick，ask 向上取 tick，且 `bid < external ask`、`ask > external bid`。

Phase 4 的 `MarketState` 只處理已正規化的 Decimal book／可識別 own sizes，不連線或自行判定 order ownership。可見價格扣除 own size 後取 external BBO；own size 大於 book、可見範圍內缺 own level、空／crossed／untrusted／時間倒退資料皆拒絕，且不再發布舊快照。Own levels 只有在提供的 depth 範圍之外才可忽略。Microprice 使用 external best sizes 加權，不可用時回 external mid。單一 EWMA 使用 sampled external-mid 絕對變動 bps，固定 1s cadence／5s half-life；第一筆為 0。這些是尚未校準的內部測試起點，沒有新增 YAML knobs。

`VolumeQuotePolicy.propose(market, account, risk, now=...)` 是純計算：要求 typed、same-symbol、trusted market（≤3s）及 authenticated account/fee（≤10s），每次使用 account 的當下 maker fee。正常報價以 reservation 為中心計算 `f + e/2 + min(5 bps, multiplier × ewma_move_bps)`，outward tick rounding／passive clamp 不縮小 fee floor；零 fee/edge/vol 恰好同 tick 時向外加一 ask tick，保持不自交叉。Entry price／單筆損益不決定是否報價。

`InventoryDecision.buy_capacity/sell_capacity` 預設為 0，表示不授予該側報價額度；非零值是**最終 desired quote 的總量上限**，不是每張新單的剩餘可加量。Policy 另限制 order size／hard position、向下取 size step，低於 minimum 就省略，不自動放大。`SKEWED` 的增加風險側至多原 order size 的一半，reducing 側不因尚未 flat 停掉；soft 分類由 Phase 5 governor 擁有。Hard 恰好命中但未給 `REDUCE_ONLY` 時拒絕；超過 hard 不產生 maker plan，必須交由 governor flatten。

報價使用依輸入有效位數／指數跨度決定的 bounded Decimal context 與整數商／餘數取 tick/lot；不能先以預設精度四捨五入，再讓數量越過 capacity、hard limit 或 residual。需要超過 4096 位工作精度或無法完成 Decimal 運算時明確拒絕，這是 code-owned 輸入邊界，不是策略參數。

Policy 可渲染已給定的 `REDUCE_ONLY` 為 long 在 external ask／short 在 external bid 的單張 reducing POST_ONLY，數量不超過 residual。這是計畫 §7.2 passive-touch 退出的 fee-floor 例外，不使用 entry breakeven 價錨。`FLATTENING/COOLDOWN/SESSION_COMPLETE` 不報價；policy 只渲染，觸發與預留由 governor 擁有。Execution 仍須先處理舊單與 exact cancel proof，不能直接把 proposed plan 當成可下單授權。

- `abs(position) < soft_limit`：持續雙邊。
- soft 至 hard：risk-increasing side 縮量／外移，低於 min lot 就省略，不回到 ping-pong。
- 到 hard：僅 reducing POST_ONLY at passive touch，不受 entry breakeven 價錨限制。
- 超過 hard：flatten。新 risk-increasing quote 必須先保留「可能成交量 × stop distance/loss + taker fee」的動態風險額，projected session loss 嚴格低於 cap。

Volatility 只暴露 multiplier；cadence、half-life、buffer cap 為內部 profile。起始版沒有 adverse-selection controller；Phase 10 只在 baseline 可行／接近可行時評估一個 markout EWMA buffer。

Phase 5 的 reserve 使用以下明示保守慣例：`stop_loss_usdg / order_size` 為每單位 stop allowance，**不是新增的價格止損距離**。Worst quantity 包含 current position、全部 remaining working orders 與 proposed desired quantities 暫時共存；不假定尚未證明的撤單成功，也不以 reduce-only 標籤取得額外額度。對非零 worst quantity 預留 `max(stop_loss, worst_quantity × stop_loss / order_size)`，另加 worst taker fee（含 IOC 滑價 notional）、IOC 滑價損失及兩側 potential maker fees；再加已實現損失、當前 inventory loss 與 working orders 對當前 touch 的 adverse gap。歷史 realized profit 不抵銷當前損失。必須同時滿足 worst quantity ≤ hard 與總額 **嚴格小於** session cap，才以整數 lots 授予 quote capacity；不足時只允許減倉。共存假設可能降低容量，Phase 6 可在 exact cancel 後重新 evaluate，不能直接放大額度。

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

`InventoryGovernor` 要求 fresh same-symbol account／open-session ledger／完整 remaining-order exposure，report duration 必須對齊同一 session clock。Manager snapshot 是本地 ownership tracking 的讀值，不是新的 authenticated exchange order-list 證明；Phase 6 仍須提供協調過的 account/order reconciliation，不能用 count 相同代替 identity 核對。Fresh account/ledger 已能證明的 hold／loss／hard／session 風險會先 latch，再拒絕 stale book／unknown execution；hold 的退出時間從實際跨越 max-hold 的時刻開始，scheduled deadline 從原期限開始，不因暫停或重試重置。Session loss、max drawdown、stop、deadline 都是終止 latch。

`FLATTENING` 可暫無 `FlattenIntent`（例如 flat 但尚待撤單證明）。`bounded_exit` 必須有 per-run 授權，先 cancel＋新 account，再從 post-cancel BBO 固定整次退出的價格上限，最多 3 次 IOC／30s，且不超過 governor 已開始的 deadline。Phase 6 runner 可先在同一 deadline 內使用設定的 passive grace，預留至少 10s 給 IOC；先撤 increasing orders，僅以 fresh residual 授權 reducing POST_ONLY at passive touch。超過 hard 時跳過 grace；grace 不重置 timer，失敗退出不另開 ID 取得另一組 3 attempts。

Bridge 使用 V2 OrderManager 的公開 cancel／one-shot preparation token／exact terminal ID 證明，每次 prepare 後重讀 account 與 BBO，只能縮小 IOC quantity；超過 execution max-position 的已知殘倉以不超過該上限的 lot-aligned chunks 處理，總 attempts 仍為 3。每次 terminal 後重新 authenticated residual。初次 cancel/fill race 可重建真正的殘倉 intent，後續 unexpected growth／reversal 或任何 proof failure 一律 BLOCKED／HALTED，不盲重試。已 authenticated flat／orders 0 的撤單證明不依賴 BBO；任何非 flat IOC 都仍需新的可信 BBO。

只有 `CONFIRMED` execution result 的新 authenticated flat／orders 0 與完整 healthy zero-order snapshot，才能 `confirm_exit`；simulated、cancel-only nonflat、晚於 exit deadline 的 flat 均不可冒充成功。非終止型風險在確認後開始 cooldown；stop/deadline/session-loss 在確認後 SESSION_COMPLETE。`BoundedExitReport` 保存 FLAT／BLOCKED／DEADLINE／ATTEMPTS_EXHAUSTED、attempts 與最後可得的 account proof；失敗保留 residual，不放寬價格／時間／風險限制。

## Ledger 與驗證

Fill idempotency、out-of-order fail closed、maker/taker turnover/fee 分離。保留 gross、funding/cashflow、all-in net/cost bps、fee cover、max drawdown、avg/P95 abs inventory、age、forced flatten count/loss、quote uptime、turnover/fills per quote hour；每筆 fill 可拆 spread capture、inventory markout、flatten concession、fee。Final-flat account reconciliation 未通過時，economic 結論不可用。

Phase 3 的帳本只接受同一 symbol／同一 monotonic clock domain 的 incremental fills、mark/working-order 狀態與具獨立 ID 的 cashflows。初始帳戶須 authenticated flat／orders 0。同 order 的不同 fill ID 是 partial fills；相同金融 payload 的重送不重記，source time 必須保留；reference-price telemetry 改變不覆寫第一次歸因。衝突金融 payload 或新事件時間倒退會鎖住 ledger，保留已接受數值，不在帳本內排序修補或重置損失。

```text
fees = actual maker fee + actual taker fee
all_in_net = realized gross - fees + signed funding
all_in_net_cost_bps = -all_in_net / maker_turnover_total * 10000
fee_cover = realized gross / fees
final_equity - initial_equity - external_transfers = all_in_net
```

成本分母不含 taker turnover；funding 不美化 fee cover，入出金不算獲利。零分母回 `None`。平均成本以剩餘 cost basis 分攤 partial close，完整 flat 時以所有 accepted buy/sell 成交現金流核對 gross；不把對帳差額補成 funding。Drawdown 為觀測到的 transfer-neutral marked equity peak-to-trough；inventory average/P95 依整場時間加權、包含 cooldown/flatten。Quote uptime 是至少一側 maker order **實際 working** 的時間聯集，不能使用 proposed quote 或雙邊重複計時。

有 reference 時，gross 分解為 `spread_capture + inventory_markout - flatten_concession`；concession 保留正負號，含改善成交。標記同一 `flatten_id` 的 partial fills 算一次 forced flatten，loss 是該組 `max(0, -(realized gross - actual fee))`，不再從 net 扣第二次。Phase 5 的 `record_exit(report)` 同樣以 flatten ID 去重，有 attempt 但零成交也計一次；原本 flat、只有撤單且沒有 attempt 則不額外計 forced flatten。Attempt 數是呼叫次數，不冒充 confirmed submission 數，費用／PnL 仍只能來自 fills。缺 reference 時仍記真實成交與費用，但分解標 unavailable；有持倉時缺 mark 的 MTM 只是 last-known-reference 診斷值。

`finalize()` 是一次性 session 終點，不等自然 flat 才挑選樣本：檢查 fresh（≤10s）authenticated same-symbol 最終帳戶、帳戶時間不早於已記事件、ledger/account flat、orders 0 及 exact equity bridge；沒有任意 rounding tolerance。USDG-only Unified 保留完整 margin cash 精度，不用 6dp 摘要取代帳本現金。失敗仍回傳完整診斷報表並封存 session，final all-in/cost/fee-cover 為 `None`；成功的 `complete` 只代表財務邊界完整，不代表策略 GO。

初始與最終 AccountSnapshot、accepted fill/mark/cashflow、bounded-exit outcome 及 final report 共用 `JsonlTelemetrySink` 的一條 append-only JSONL；Decimal 以字串保存、拒絕 raw dict／未知 DTO／既有輸出檔。先 ingest 相應 fills，再 record_exit，不重記重送事件。IO 失敗只標 telemetry error，不抹掉已記金融真實性；此階段不建 restart、fsync persistence 或 campaign authority（Phase 11 另行處理）。

驗證以公開 port／行為為契約，不以 private state/reason strings 綁定策略。依序完成純函式／ledger／execution／orchestrator 測試，再覆蓋八種 replay：calm、short 遇上漲、long 遇下跌、震盪 fills、stale、cancel/fill race、deadline nonflat、IOC partial residual。Phase 7 才做 10min dry smoke → 30min dry T3；此時只判定 safety/liveness simulation，economics 未評估。

## Phase 6 runner／資料邊界

`MarketMakerV2Config` 恰有 18 個 user-facing leaves（包含 symbol/profile/dry_run）；金融值為 quoted Decimal strings。唯一 profile 是 `fee_neutral_volume_v1`，book/account 3s/10s、IOC 3 attempts/30s 是 code-owned bounds，並非 YAML 安全開關。Example 預設 dry；sample 數值不是 live 建議。CLI 在讀取 exchange settings、建 adapter 或連線前拒絕缺少 `--authorize-bounded-flatten` 的 live config。

啟動須驗證 robinhood network/testnet 對應、明確 expected L1 owner/account index、USDG Classic 或下述窄 Unified／cross 1x、全帳戶僅目標 symbol、flat／orders 0、fresh current fee 及 market minimum；不自動調 leverage/margin、不放大 order size。操作者仍須保證沒有其他 bot／手動策略或自有帳戶會與本 run 互成交；起始快照不能證明未來的獨占性。Funding discount 不當作已收現金；其他 collateral／模式／入出金目前不支援，對帳不符即停止。

Account read 用 fresh account_all／authenticated account_all_orders 前後夾住 REST cash，核對 account identity、assets／position core fields、funding fingerprint、累積 trade count 與完整訂單；不能用 count 相同代替 exact IDs、side、price、remaining、reduce-only。初始及 trade count 增加時才讀 REST trades；新 IDs 必須恰等於 counter delta，sequence／actual fee／ledger position-equity 都須一致，100-row 窗口不足即拒絕。Counter 對應的 history checkpoint 與已 ingest ledger IDs 分離，失敗重讀不永久卡住也不重複計費。新 terminal order 仍查 exact terminal status／filled quantity，不能用可能落後的 flat REST 取代。

Book 使用 RH 官方 nonce 契約：initial full snapshot，後續 begin_nonce 必須等於前次 nonce，offset 單調增加；source timestamp 不得未來／倒退／超過 3s，receipt 亦不能超過 3s。主機時間不合即拒絕，不自動推算偏移。Windows使用同域perf_counter/QPC作session、account、market及OM monotonic；source epoch時間仍獨立檢查。僅一個host wall-clock quantum內的future packet可在receiver等待一次（cap20ms），再執行原strict0..3000ms；等待前receipt保留，不接受仍future／已stale資料。完全相同且仍fresh的book/own snapshot可重讀，不重寫時間；不同內容仍须新嚴格遞增receipt。Book data failure 永久封鎖行情但保留正常 account transport；真正 transport／identity／protocol failure 全部失效。失敗收尾才明確關閉 read stream 並使用有界 REST 查證，market 仍不可用，不恢復報價或無價 IOC。Normal wrapper 每次重新 evaluate，僅同 cycle／相同 ownership-exposure key 可共用 fresh account；post-cancel 必須新現金查證。3s cycle 不新增 YAML knob；stop 獨立記時，慢讀取不延後 30s 退出界線。最後仍需 bounded cleanup、獨立 authenticated postflight、ledger finalize 與 disconnect。

Dry 只保存 simulated working intents，actual account/ledger 仍是零交易真實讀值；final report 刻意不標 complete，CLI `economics_evaluated=false`。Live 的 report.complete 只表示 exact final-flat accounting；未完成帳務或 cleanup 的 run 不得 completed。Quote uptime 由已觀測的 authenticated orders／confirmed working execution 更新，不是 proposed plan；缺 fresh reference 時保留可證明的觀測區間，不造 mark。

一條 exclusive JSONL 保存 typed events；SDK logging 在 CLI run 範圍停用並恢復，避免 raw responses／credentials 進入輸出。Phase 7 前提：確認獨占帳戶設定後才做唯讀 dry smoke → dry T3。REST consistency read 可能在正常 partial-fill race 時拒絕並觸發安全收尾，呼叫頻率／延遲／這類 false No-Go 的實際 liveness 尚未驗證；不能把離線 runner 通過解讀成連續實盤已穩定。

Phase 7 已補齊計畫的 8 個 replay，使用真實 V2 ports／OrderManager 與離線 fixtures。既有 `InventoryDecision` 現在直接寫入同一 JSONL，沒有新增 DTO／設定／分析框架。Dry simulated quote uptime 從相鄰 execution snapshot 時間積分，revisions 由同次 simulated cancel＋create 計數；不得使用真實 ledger 的 quote uptime（dry 無真單故為 0）代替。資源使用以外部 process 讀值觀察，尚未執行 timed dry 就不報 stability GO。

2026-09-04 初次 preflight 因 Unified=1／margin balance 不等於 collateral 拒絕。後續唯讀驗證確認 margin cash 多保留小數位，而不是額外資產；[官方 RH 前端](https://robinhoodchain.lighter.xyz/assets/index-Czf76op5.js) 直接用 `margin_balance × index_price × loan_to_value` 計 cross collateral，並以 settlement margin cash＋PnL 計 equity，不能再加一次 collateral。[公開 assetDetails](https://api.rh.lighter.xyz/api/v1/assetDetails) 當時確認 USDG asset 3、decimals 6、index price／LTV 皆 1。

Unified 支援只限 sole USDG、spot／locked balance 0、margin enabled、無 pool shares／pending unlock／isolated orders／allocated margin、無其他 symbol position 或 PnL；保留 cross 1x 及 exact order proofs。Fee/funding 與 settlement metadata 最多快取 8s，原始 request-start time 保存在獨立 inputs_observed_monotonic；account cash 仍每次新讀，兩者都須 ≤10s。Funding fingerprint 變化立即刷新；要求唯一 matching asset ID、USDG 精度 6 及單位估值，帳戶模式不能在 session 中改變。現金用完整 `Decimal(margin_balance)`；equity 為現金＋authenticated unrealized PnL，需要非精確捨入的 equity／bridge 運算拒絕。6dp `ROUND_DOWN(cash)` 必須等於 collateral，`ROUND_DOWN(cash + PnL)` 必須等於 total／cross asset value；這只是針對已觀測摘要關係的保守 compatibility guard，**不是官方保證的 rounding 規格**，更不是帳本誤差容忍。獨立四捨五入的 nonflat REST PnL 可能造成 false No-Go，不可自行放寬；nonflat 真實帳務／funding liveness 仍是後續 live gate 的未驗證事項。本輪僅 flat dry，不能據此宣稱 live-ready。Code-owned refusal 可輸出原因，raw SDK errors 仍遮蔽。

首輪 smoke 的 HTTP 429 證據保留於 EXPERIMENT_LOG。讀取重構不更改 Premium tier（authenticated accountLimits 已核對）、shared retry/pacing 或風險配置；[RH rate limits](https://apidocs.rh.lighter.xyz/docs/rate-limits.md) 為 Premium 24,000 weighted REST／rolling minute，WS outgoing 上限 200/min。正常 flat dry 的目標為 3s/cycle，一次 fresh account bracket／cycle，fee/funding 與 metadata 分別保留原時間；quiet nominal REST 約 13,200/min，另留收尾讀值，不以慢輪詢取巧。WS 按 [官方契約](https://apidocs.rh.lighter.xyz/docs/websocket.md) 取得 full snapshot：account_all 可直接重订閱；account_all_orders 實測直接重订閱會回 30003，必須先 unsubscribe 並確認相同 channel，再取得新 authenticated full snapshot，合計每次重取兩則 outgoing。每個 request 在單一 timeout／lock 內完成，未知 ack／timeout 不重試或當成功。實測讀取計數與 rolling 峰值只記唯一 experiment log。

Live read-budget 仍為 No-Go：合法高改價離線 fixture 在加入 unsubscribe 前就曾達 rolling60 REST 26,700、WS 192（未含 ping/pong）；同一 fixture 換算現版 unsubscribe 握手約 308 WS/min，明確超出 200；不能把 flat dry 成功當成改價／terminal-history／最多三次 IOC 共存的 reserve 證明。進 Phase 8 前須以完整 mutation/reconciliation/exit 讀取路徑修正並驗證 reserve，且仍需逐場新 live＋bounded-flatten 授權。Inventory tuning 在 canary 可行後才開始；2h/4h/24h 依計畫順序，不沿用 V1 Gate A/B/C。V1 移除不改變這些 gate；shared Lighter 改動必須執行 Grid 回歸。
