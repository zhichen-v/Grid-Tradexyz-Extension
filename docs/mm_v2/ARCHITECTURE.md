# Market Maker V2 — Architecture contract

> 設計契約，逐階段實作。權威來源：[rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)。產品目標／fee floor／授權／complexity budget 見 [OBJECTIVE](OBJECTIVE.md)。Phase/run 驗收只記在 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

## 2026-09-05 review：契約與實作落差

下文描述設計契約及既有接線，不能視為全部已驗證。`260be69` 的離線review確認 cleanup、passive exit、POST_ONLY recovery、持續雙邊報價、own/book對齊、current drawdown reserve與final inputs freshness共七項缺口。後續授權修復已修正R1／R2與F5；R3正常arrival race、保守hold age與API admission已有離線驗收，30min T3及真實nonflat仍未通過。具體重現、驗收與進度見 [rebuild plan §19](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md#19-2026-09-05-review從目前-phase-7-接續)。

Normal quote bridge 在同一10s deadline內最多兩次one-create reconcile；每次均需新授權，首筆成交後重新核對持倉再補單。Read refusal不自行清除OM uncertainty，也不永久禁用已知單cleanup。POST_ONLY rejection generation只由其後的新可信book acknowledge，原cooldown仍有效。Passive grace失敗後使用同一bounded exit deadline／attempt budget收尾。Governor以current drawdown補足high-water剩餘風險預留，歷史max drawdown仍用於stop／診斷。

Live接線及Unified nonflat帳務仍需逐場授權canary；flat dry不證明成交／改價能持續運行。經濟主指標為固定完整時間的maker turnover；`scripts/analyze_mm_v2_session.py`重播既有ledger、保留失敗窗口、以總分子除總分母聚合成本。Dry／replay、缺final proof或未對帳的資料不發布actual economics，allocated capital必須另給明確輸入，不能用約299USDG帳戶背景值自動分配。

Phase 2–5 的 immutable models、五個 ports、ledger、quote policy、governor 與 bounded execution 已接入 Phase 6 `VolumeSession`／獨立 runner。2026-09-06 刪除已被取代的空計畫 helper／dry port；`DryVolumeExecutionPort` 支援非空報價意圖但不虛構成交。必要 execution safety 由 V2 自有模組承接，不載入或保存 V1 package。實際 rollout 證據以 EXPERIMENT_LOG 為準。

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

Phase 5 的 reserve 使用以下明示保守慣例：`stop_loss_usdg / order_size` 為每單位 stop allowance，**不是新增的價格止損距離**。Capacity 與 policy／execution 一致，表示每側目標總量：每側曝險取 `max(working_remaining, desired_total)`，worst quantity 再取 current position、position＋buy exposure、position−sell exposure 的最大絕對值；maker fee也預留兩側較大總量。前提是V2每側只有一槽，替換前必須exact terminal cancellation與fresh重新授權；保留買側而新增賣側仍同時預留兩側，未知單或不明mutation不得使用此契約。舊單在證明撤除前始終計入，仍保留舊單價格與adverse gap；舊單本身不安全時拒絕新增風險，不假定撤單成功，也不以reduce-only標籤增加額度。對非零worst quantity預留 `max(stop_loss, worst_quantity × stop_loss / order_size)`，另加worst taker fee（含IOC滑價notional）、IOC滑價損失及兩側potential maker fees；再加已實現損失、當前inventory loss與working orders對touch的adverse gap。歷史realized profit不抵銷當前損失。必須同時滿足worst quantity≤hard與總額**嚴格小於**session cap，才以整數lots授予capacity；不足時只允許減倉。

2026-09-06完整60min live揭露舊實作把capacity算成old以外的增量，但policy把它當總量，累積損失後反覆`.00036 target → cancel → .00040 empty-account target`，補第二邊前又撤掉第一邊。本輪改為上述一致的總量契約，不放寬任何設定上限；實際terminal取消後仍必須重新evaluate。使用該場損失／費率／lot的真實port＋governor離線對照，舊碼只留下BUY，修後雙邊同ID維持11個5s cycle，60s到期才正常撤換；不能將這個離線結果當作修後實盤volume或fee-cover證據。

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

Maker minimum 與 quantity step 是不同契約。[Lighter 官方交易文件](https://apidocs.lighter.xyz/docs/trading) 明示 min base／min quote 僅適用 maker；bounded reduce-only IOC 可使用低於這兩個 minimum 的正數整 lot。2026-09-07 修正 `_exit_intent` 與 `_validate_active_unwind` 的錯誤下限，讓 BTC `.00017` 殘倉（maker minimum `.00020`、step `.00001`）及再次 partial 後的 `.00001` 可精確退出。IOC 仍保留 reduce-only、fresh account／book、position cap、tick／step、固定 limit／deadline 與 terminal proof；不能湊大數量，正常 POST_ONLY（含 reducing maker）仍須符合 maker minima。

`InventoryGovernor` 要求 fresh same-symbol account／open-session ledger／完整 remaining-order exposure，report duration 必須對齊同一 session clock。Manager snapshot 是本地 ownership tracking 的讀值，不是新的 authenticated exchange order-list 證明；Phase 6 仍須提供協調過的 account/order reconciliation，不能用 count 相同代替 identity 核對。Fresh account/ledger 已能證明的 hold／loss／hard／session 風險會先 latch，再拒絕 stale book／unknown execution；hold 的退出時間從實際跨越 max-hold 的時刻開始，scheduled deadline 從原期限開始，不因暫停或重試重置。Session loss、max drawdown、stop、deadline 都是終止 latch。

`FLATTENING` 可暫無 `FlattenIntent`（例如 flat 但尚待撤單證明）。`bounded_exit` 必須有 per-run 授權，先 cancel＋新 account，再從 post-cancel BBO 固定整次退出的價格上限，最多 3 次 IOC／30s，且不超過 governor 已開始的 deadline。Phase 6 runner 可先在同一 deadline 內使用設定的 passive grace，預留至少 10s 給 IOC；先撤 increasing orders，僅以 fresh residual 授權 reducing POST_ONLY at passive touch。超過 hard 時跳過 grace；grace 不重置 timer，失敗退出不另開 ID 取得另一組 3 attempts。

若fresh account已flat且orders0，但兩側capacity皆低於venue minimum，已沒有可執行的新風險容量：latch終止並沿上述bounded exit取得新證據，才SESSION_COMPLETE，CLI明示`stop_reason=risk_capacity_exhausted`。不保持REDUCE_ONLY空計畫到時長結束、不提高loss limit；有庫存仍可減倉，有舊單造成預留不足則先撤後fresh重新評估。這種提前收尾保留原定經濟窗口分母，不能算完整長測通過。

Bridge 使用 V2 OrderManager 的公開 cancel／one-shot preparation token／exact terminal ID 證明，每次 prepare 後重讀 account 與 BBO，只能縮小 IOC quantity；超過 execution max-position 的已知殘倉以不超過該上限的 lot-aligned chunks 處理，總 attempts 仍為 3。每次 terminal 後重新 authenticated residual。初次 cancel/fill race 可重建真正的殘倉 intent，後續 unexpected growth／reversal 或任何 proof failure 一律 BLOCKED／HALTED，不盲重試。已 authenticated flat／orders 0 的撤單證明不依賴 BBO；任何非 flat IOC 都仍需新的可信 BBO。

退出讀取會清除前一cycle的opening/confirmation/audited order handoff，保留OM preparation generation，確保新opening與nonce-aligned book的receipt在prepare之後；不能把仍在3s內但早於prepare的book直接使用。原52 proof frames已按8個完整account audit×5與3次IOC×4計算，這個修正不增加預留上界。Exit market的metadata、時間與價格界線分開診斷；只記白名單數值。Normal API背壓的收尾在except handler之外執行，避免cleanup失敗的implicit context誤指已處理的quota拒絕。

每個account snapshot仍最多兩次完整audit、共享10s；normal階段耗盡的`AccountReadRace`可以沿同一收尾／API headroom等待／fresh strict reauthorization路徑恢復，前提是live budget啟用、非exit期、stream transport健康，且所有已知訂單可核對。正常quote port保留此型別到runner，包含第一側已確認送出後的第二側refresh。只有精確的activity race基底型別可恢復；cash race子型別、一般讀取錯誤／identity或terminal證據衝突、未知mutation、exit/IOC/final失敗不適用。既有ledger、損失與session deadline不重置；任何新POST_ONLY前都須新strict account proof。Sidecar的`account_read_deferrals`與API `deferrals`分開，成功恢復不產生FailureDiagnostic；收尾失敗仍保留真實診斷，不反覆嘗試或將舊flat proof當現況。

2026-09-06 20:20本地長測候選的ioc_slippage_ticks為200（前場20），BTC tick0.1對應初次退出BBO±20 USD/BTC；後續IOC固定同limit，不逐次放寬。最大倉位0.00080的整次price concession最多0.016 USDG，另計fee/funding及退出前損失；governor已依同ticks預留價差與taker fee，session/inventory loss limits不變。一般example/default未改；200ticks是本次候選而非必定平倉或fee-cover保證，候選比較與舊場證據見EXPERIMENT_LOG。

只有 `CONFIRMED` execution result 的新 authenticated flat／orders 0 與完整 healthy zero-order snapshot，才能 `confirm_exit`；simulated、cancel-only nonflat、晚於 exit deadline 的 flat 均不可冒充成功。非終止型風險在確認後開始 cooldown；stop/deadline/session-loss 在確認後 SESSION_COMPLETE。`BoundedExitReport` 保存 FLAT／BLOCKED／DEADLINE／ATTEMPTS_EXHAUSTED、attempts 與最後可得的 account proof；失敗保留 residual，不放寬價格／時間／風險限制。

2026-09-06 funding 修復將退出證明與經濟對帳分開：僅 bounded exit 的 account read 可容許尚未完成歸因的 cash bridge gap；fresh identity、position 與已接受 fills 一致、完整 known-order ownership／terminal proof、current fees、帳戶結構及 source checks 全部保留。IOC 仍須新鮮可用行情與原 reduce-only／價格／數量／期限界線。退出讀取可暫略尚無精確 round 證據的新 funding，不把 rounded change 入帳，也不把 ID 標成已接受；正常報價、恢復報價與獨立 final read 仍要求 strict exact cash bridge。CLI 另呈現 `cleanup_authenticated/position/open_orders`，不能將 cleanup 0/0 冒充 final economic proof。

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

成本分母不含 taker turnover；funding 不美化 fee cover，入出金不算獲利。零分母回 `None`。平均成本以剩餘 cost basis 分攤 partial close，完整 flat 時以所有 accepted buy/sell 成交現金流核對 gross；不把對帳差額補成 funding。Drawdown 為觀測到的 transfer-neutral marked equity peak-to-trough；inventory average/P95 依整場時間加權、包含 cooldown/flatten。Quote uptime 是至少一側 maker order **實際 working** 的觀測時間聯集，不能使用 proposed quote 或雙邊重複計時。MarkEvent另保存已證明的quote_sides，ledger報buy／sell／two-sided seconds，滿足union=buy+sell−two-sided；舊事件只知quoting而無side時，側別統計為None，不從quote plan補造。這些時間以觀測點分段，未宣稱具有exchange事件時間精度；網路dry的真實working時間仍為0。

有 reference 時，gross 分解為 `spread_capture + inventory_markout - flatten_concession`；concession 保留正負號，含改善成交。標記同一 `flatten_id` 的 partial fills 算一次 forced flatten，loss 是該組 `max(0, -(realized gross - actual fee))`，不再從 net 扣第二次。Phase 5 的 `record_exit(report)` 同樣以 flatten ID 去重，有 attempt 但零成交也計一次；原本 flat、只有撤單且沒有 attempt 則不額外計 forced flatten。Attempt 數是呼叫次數，不冒充 confirmed submission 數，費用／PnL 仍只能來自 fills。缺 reference 時仍記真實成交與費用，但分解標 unavailable；有持倉時缺 mark 的 MTM 只是 last-known-reference 診斷值。

`finalize()` 是一次性 session 終點，不等自然 flat 才挑選樣本：檢查 fresh（≤10s）authenticated same-symbol 最終帳戶、帳戶時間不早於已記事件、ledger/account flat、orders 0 及 exact equity bridge；沒有任意 rounding tolerance。USDG-only Unified 保留完整 margin cash 精度，不用 6dp 摘要取代帳本現金。失敗仍回傳完整診斷報表並封存 session，final all-in/cost/fee-cover 為 `None`；成功的 `complete` 只代表財務邊界完整，不代表策略 GO。

`FailureDiagnostic`也寫入同一JSONL，記錄stage、錯誤類型、V2模組/行號、已知訂單slot狀態及uncertain/unknown旗標。先在execution port吞例外轉BLOCKED前記原錯誤，再保留退出/final-account/disconnect的後續錯誤；不序列化exception message、locals、SDK payload或帳戶設定。診斷capture/sink失敗不得中斷cleanup。Analyzer接受final report之後的disconnect診斷，但不接受其他財務事件續寫；有failure diagnostic或CLI run失敗時不宣稱economic驗證完成，實際成本事件仍保留。

初始與最終 AccountSnapshot、accepted fill/mark/cashflow、bounded-exit outcome 及 final report 共用 `JsonlTelemetrySink` 的一條 append-only JSONL；Decimal 以字串保存、拒絕 raw dict／未知 DTO／既有輸出檔。先 ingest 相應 fills，再 record_exit，不重記重送事件。IO 失敗只標 telemetry error，不抹掉已記金融真實性；此階段不建 restart、fsync persistence 或 campaign authority（Phase 11 另行處理）。

驗證以公開port／行為為契約，不以private state/reason strings綁定策略；既有測試覆蓋純函式、ledger、execution、runner與八種replay。早期Phase7安排10min smoke→30min T3；目前依rebuild plan §19.7改為受影響測試＋5分鐘本機dry，已被完整suite覆蓋的測試不重跑。長測仍是未完成的持續運行證據，不阻擋準備最小canary；dry只提供模擬safety/liveness證據，不判定economics。

## Phase 6 runner／資料邊界

`MarketMakerV2Config` 恰有 18 個 user-facing leaves（包含 symbol/profile/dry_run）；金融值為 quoted Decimal strings。唯一 profile 是 `fee_neutral_volume_v1`，book/account 3s/10s、IOC 3 attempts/30s 是 code-owned bounds，並非 YAML 安全開關。Example 預設 dry；sample 數值不是 live 建議。CLI 在讀取 exchange settings、建 adapter 或連線前拒絕缺少 `--authorize-bounded-flatten` 的 live config。

啟動須驗證 robinhood network/testnet 對應、明確 expected L1 owner/account index、USDG Classic 或下述窄 Unified／cross 1x、全帳戶僅目標 symbol、flat／orders 0、fresh current fee 及 market minimum；不自動調 leverage/margin、不放大 order size。操作者仍須保證沒有其他 bot／手動策略或自有帳戶會與本 run 互成交；起始快照不能證明未來的獨占性。Funding discount 不當作已收現金；其他 collateral／模式／入出金目前不支援。正常 cash bridge 不符會在原 10s 內強制更新一次 funding／trades 證據，仍不符則停止新增風險，沿上述退出契約收尾。

Account read 用兩端 authenticated target-market account_orders 完整訂單夾住 REST cash；cash 後只取一次 fresh account_all，仍核對全帳戶 identity、assets／position core fields、所有市場 order counts、funding fingerprint 與累積 trade count。Opening／closing exact IDs、side、price、remaining、reduce-only 必須一致；不能用 count 相同取代。OM sync 的完整 observation 可交給 account 作 opening，完成 audit 的 closing 可供 OM sync 使用一次；限同一 cycle／mutation generation／3s內，保留 request-start，new cycle／mutation／失敗立即作廢，不改 terminal／IOC 獨立證明。初始及 trade count 增加時才讀 REST trades；新 IDs 必須恰等於 counter delta，sequence／actual fee／ledger position-equity 都須一致，100-row 窗口不足即拒絕。Counter 對應的 history checkpoint 與已 ingest ledger IDs 分離，失敗重讀不永久卡住也不重複計費。新 terminal orders 以一次100-row history核對每個 exact ID／terminal status／filled quantity；重複、缺漏、窗口不足或成交量不符即拒絕，已確認終態不重讀，晚到fills仍須與immutable proof一致。不能用可能落後的flat REST取代。

Book 使用 RH 官方 nonce 契約：initial full snapshot，後續 begin_nonce 必須等於前次 nonce，offset 單調增加；source timestamp 不得未來／倒退／超過 3s，receipt 亦不能超過 3s。主機時間不合即拒絕，不自動推算偏移。診斷記錄source age及wall/monotonic elapsed差異；source age仍在界線內但相鄰elapsed差額超過50ms也視為clock jump，停用行情並保留account cleanup transport。50ms是主機連續性界線，不是UTC誤差容忍；Windows／Linux共用此條件。Windows使用同域perf_counter/QPC作session、account、market及OM monotonic；source epoch時間仍獨立檢查。僅一個host wall-clock quantum內的future packet可在receiver等待一次（cap20ms），再執行原strict0..3000ms；等待前receipt保留，不接受仍future／已stale資料。完全相同且仍fresh的book/own snapshot可重讀，不重寫時間；不同內容仍须新嚴格遞增receipt。Book data failure 永久封鎖行情但保留正常 account transport；真正 transport／identity／protocol failure 全部失效。失敗收尾才明確關閉 read stream 並使用有界 REST 查證，market 仍不可用，不恢復報價或無價 IOC。Quote book必須在opening order request之後實際收到，且opening nonce ≤ book nonce ≤ closing nonce、兩端完整own orders相同；最多等待3s，不補時間戳、不盲扣舊book的own size。超時或超出兩端的book不能報價，但已成立的authenticated flat證明仍供cleanup使用。Aligned packet每次重用仍透過stream檢查其原始source timestamp和目前行情健康；較新的book fresh不會讓舊packet延壽，book fault也不能被healthy account transport掩蓋。Normal wrapper每次重新evaluate，僅同cycle／相同ownership-exposure key可共用fresh account；mutation generation變化及任何可見訂單／financial-state變動都使cash cache失效；退出與final必須新現金查證。3s cycle 不新增 YAML knob；stop 獨立記時，慢讀取不延後 30s 退出界線。最後仍需 bounded cleanup、獨立 authenticated postflight、ledger finalize 與 disconnect。

Dry 只保存 simulated working intents，actual account/ledger 仍是零交易真實讀值；final report 刻意不標 complete，CLI `economics_evaluated=false`。Live 的 report.complete 只表示 exact final-flat accounting；未完成帳務或 cleanup 的 run 不得 completed。Quote uptime 由已觀測的 authenticated orders／confirmed working execution 更新，不是 proposed plan；缺 fresh reference 時保留可證明的觀測區間，不造 mark。

一條 exclusive JSONL 保存 typed events；SDK logging 在 CLI run 範圍停用並恢復，避免 raw responses／credentials 進入輸出。Phase 7 前提：確認獨占帳戶設定後才做唯讀 dry smoke → dry T3。REST consistency read 可能在正常 partial-fill race 時拒絕並觸發安全收尾，呼叫頻率／延遲／這類 false No-Go 的實際 liveness 尚未驗證；不能把離線 runner 通過解讀成連續實盤已穩定。

Phase 7 已補齊計畫的 8 個 replay，使用真實 V2 ports／OrderManager 與離線 fixtures。既有 `InventoryDecision` 現在直接寫入同一 JSONL，沒有新增 DTO／設定／分析框架。Dry simulated quote uptime 從相鄰 execution snapshot 時間積分，revisions 由同次 simulated cancel＋create 計數；不得使用真實 ledger 的 quote uptime（dry 無真單故為 0）代替。資源使用以外部 process 讀值觀察，尚未執行 timed dry 就不報 stability GO。

2026-09-04 初次 preflight 因 Unified=1／margin balance 不等於 collateral 拒絕。後續唯讀驗證確認 margin cash 多保留小數位，而不是額外資產；[官方 RH 前端](https://robinhoodchain.lighter.xyz/assets/index-fat83qVw.js) 直接用 `margin_balance × index_price × loan_to_value` 計 cross collateral，並以 settlement margin cash＋PnL 計 equity，不能再加一次 collateral。[公開 assetDetails](https://api.rh.lighter.xyz/api/v1/assetDetails) 當時確認 USDG asset 3、decimals 6、index price／LTV 皆 1。

Unified只支援sole USDG、spot/locked balance0、margin enabled、無pool shares/pending unlock/isolated orders/allocated margin、無其他symbol倉位或PnL、cross1x。Fee/funding及settlement metadata保留原8s request-start；帳戶輸入≤10s，模式不得在session中改變。唯一cash authority是完整Decimal(margin_balance)，equity=cash+同response的unrealized PnL；正常與final的exact ledger bridge兩側PnL抵銷，仍逐位核對baseline cash＋實際fill/funding/fee，不加入epsilon或從cash gap補造funding。保留USDG decimals6、index/LTV1、cash非負及ROUND_DOWN(cash,6dp)==collateral、finite total/cross且相等。Flat依position==0判斷，必須PnL0、total/cross==截斷cash；nonflat不再要求ROUND_DOWN(cash+serialized PnL)==valuation summary，因無共同mark/rounding的官方契約。這是估值可信來源的設計修正，不是宣稱已證明後端rounding bug；[官方PnL定義](https://docs.lighter.xyz/trading/pnl-and-total-account-value)與[RH前端](https://robinhoodchain.lighter.xyz/assets/index-fat83qVw.js)支持cash加mark PnL，未保證上述序列化等式。Governor既有fresh touch loss與API PnL loss取較保守值，樂觀API估值不能隱藏touch虧損；final0/0及exact cash bridge不變。

V2 constructor 明確 opt in `enable_market_maker_exact_funding()`；Grid/default fee/funding 輸出不改。第一次成功讀取只將 authenticated `positionFunding` 歷史作 baseline，秒級 timestamp 轉成與 trades 相同的毫秒，保存每個 ID 的不可變來源欄位，不查 public rounds、不把開場前 funding 算進本場損益。之後新 ID 以同 network／market、相同時間的 public `/fundings` 1h round 取 `value`，乘 authenticated position size，依 position side 與付款方 direction 決定正負；同時要求 public 百分數 rate／100 與 authenticated signed rate 一致，完整金額的 `ROUND_DOWN(6dp)` 等於 authenticated `change`。Discount 只驗證、不加入現金。一次 discovery 最多一個有界 range query；完整成功才提交快取，來源衝突不能被快取遮蔽，已精確核對的 ID 不重查。

`value` 作每單位部位 funding cash 的解讀，是由[官方 funding 公式](https://docs.lighter.xyz/trading/funding)、authenticated round 與獨立 public round 資料支持的工程推論；[官方 SDK 欄位表](https://github.com/elliottech/lighter-python/blob/main/docs/Funding.md)沒有明示其語義。[RH funding chart](https://robinhoodchain.lighter.xyz/assets/FundingRateChart-BPg8hN59.js)支持 public rate 的百分數單位與 direction 付款方。這條 bridge 不使用現金差額反推 funding；正常與 final 的完整現金對帳仍須逐位成立，public 證據缺漏不能改用六位摘要或 epsilon。

首輪 smoke 的 HTTP 429及各次API模型失敗證據保留於EXPERIMENT_LOG。Live V2在啟用admission時要求fresh authenticated `accountLimits.user_tier == premium`，缺失／其他tier拒絕；read-only及Grid原預設不改。[RH rate limits](https://apidocs.rh.lighter.xyz/docs/rate-limits.md)先前確認Premium REST24,000 weighted／rolling60、WS outgoing200／min；[目前主網文件](https://apidocs.lighter.xyz/docs/rate-limits)可交叉核對權重，但不冒充本次重新取得RH規格。Account_all直接重新subscribe，account_orders則unsubscribe＋matching ack＋subscribe，後者每次重取兩則outgoing。Active-budget normal cycle為5s，其餘為3s。

`ApiBudget`只計owned Python REST transports與owned WebSocket所有frames（含control／ping／pong／close），不記URL query、headers或body；未知endpoint拒絕。Live在connect/authenticate後、開stream前等待60s，使無法截取的native signer startup請求退出rolling window；stop會中斷等待。其他IP／L1 consumers不在本meter內，操作者須維持獨占。Runner另存exclusive `.jsonl.budget.json`，檔案已存在即拒絕，不覆寫證據。

Normal account audit、額外order read、OM sync及mutation前皆做admission；post-send terminal confirmation不被normal guard截斷。Stream audit起始保守檢查REST1000／WS5（cash300、trades600、terminal history100）；條件性REST在**實際查詢之前**再分步檢查：fees900（兩個authenticated endpoint600＋最多一個public funding round300）、settlement300、trades600、terminal history100。快取仍有效或沒有新交易／終態時，對應query與分步gate均省略；8s TTL、原始request-start及exact帳戶證據不變。這些gate不是已扣款／持有的reservation，故metadata之後的trades/history不可省略自己的檢查，也不能將gate數字相加當作實際wire用量。任一步拒絕都不得發送該query；settlement只在成功後原子更新值和request-start，避免舊值被標成fresh。無stream的forensic雙讀不屬於1000 audit上界，各實際fees/settlement/trades仍經既有callback。

退出預留保持**健康transport下30s／最多3次IOC，加10s獨立final proof、一次coherent retry、一次forced funding refresh與一次精確public round**，不是任意多次429／race／斷網必成功的保證。REST前綴為 `E(t)=6006+100×min(33,floor(2t))+900×min(5,1+floor(t/8))+900+1000`（0≤t≤40）；以exact Fraction檢查poll／metadata TTL及既有請求過期的邊界，不提早挪用未到期quota。立即保留 REST8806／WS67／TX5；每次normal gate均將當次action成本在整個horizon保守保留。6006含8cash、4trades、9first history、market metadata與nonce；33次extra history poll間隔至少0.5s，最後的900含forced fee/funding600及public round300。Exit中的新分步kinds使用已含的預留，不重複預審普通工作；超出一次的retry／forced funding仍須讀取前重新admit，原deadline不變。此live opt-in取消REST429自動重試，原Grid/default重試不改；429即表示健康額度假設失效並安全收尾。

完整account audit（含terminal history）最多兩次嘗試，共用原10s deadline與mutation generation。可重讀情境包括stream/bookend/counter/partial-fill race、exact terminal history缺失、fills與position暫不一致、合法終態的fills晚到，以及可能先於funding歷史到達的cash bridge變化。兩次間清本cycle observation並禁cash reuse；一般arrival保留metadata原TTL，cash bridge重讀則強制刷新fee/funding與trades，不延長deadline。錯identity、duplicate history、invalid terminal quantities、immutable proof衝突及上述Unified cash/collateral或flat summary不符仍硬拒絕。已接受fill逐筆以ID去重，完整proof成功才更新stream checkpoint。先前因第二讀可能追加history100，retry REST由900改1000、退出預留8406改8506；本輪再加入public funding300，立即預留為8806，不增加重讀次數、WS或deadline。

Active-budget session跳過未納入配額上限的optional passive grace，直接沿原期限做bounded exit。SDK的reduce-only IOC直接查positive exact terminal history，驗exchange/client ID、symbol、side、quantity、price、reduce-only及IOC身份；缺漏仍uncertain且不得重送。MM exact cancel的history-only證明在stream關閉後也保留，方便已知單cleanup；Grid預設不改。已正面確認cancel後的quota read拒絕不抹掉execution health，mutation uncertainty與真正deadline/cancellation仍保留HALT。Terminal session exit已確認後直接走獨立final proof，不重新進入normal admission消耗保留額度。高改價fixture仍可能因quota保留提早停止；安全停止不代表達成持續大成交量。

正常account audit的arrival／cash bridge重讀共用上述一次機會與原10s期限；已接受fill按ID去重，完整proof才推進stream checkpoint。未知／malformed／counter窗口不足不重試；cash bridge在強制取得funding證據後仍不符則拒絕。成功cash proof只在cached positions全zero、同generation、原request-start<8s、opening/closing完整訂單不變、全量account_all financial state（含PnL）及trade counter不變時重用，不能restamp；任何不符改用fresh REST重驗，失敗／取消即作廢。Fee/funding一般以實際取得的source fingerprint及原8s期限快取，cash bridge重讀明確跳過TTL。初始／exit／final仍fresh REST；WS缺少可證明mode等完整欄位，不能永久取代現金取證。持倉首次觀察或方向反轉時，governor age由前一次成功cash request-start保守起算；晚到fill不能把最大hold重新歸零，ledger ingestion age另作診斷。

VPS 上線前在**實際執行主機**核對 `timedatectl show -p NTPSynchronized -p NTP`、`chronyc tracking`（若使用chrony）、UTC時間與相同V2 revision／`.venv`。Windows校時不會設定VPS；已有正常chrony時不疊加第二個NTP服務。現有strict book來源／receipt／clock-jump檢查必須在新主機重新經過唯讀preflight與dry驗證，不能只憑NTPSynchronized=yes宣稱行情可信。任一主機校時或部署都不授權live；保持獨占帳戶與逐場bounded-flatten授權。

Submission lookup→OM sync→account opening共用的完整order observation仍限同cycle／generation／3s、一次消費。Active partial-filled quantity另與trade history的exact cumulative amount核對；cash與counter一致落後也不能冒充flat。Book source checker先驗目前live book，再驗要求重用的歷史aligned packet；只因舊packet過期時拒絕該packet，不污染仍fresh的目前book，真正live來源失效仍永久停用行情。


## 本機快速驗收

依2026-09-06使用者指示，VPS暫緩；日常使用受影響的離線契約測試＋5分鐘本機dry，不再等待30分鐘T3才能測策略。長測留待延長運行前；短flat dry不證明nonflat/live或fee-neutral。

CLI明確加入`--allow-delayed-dry-book`時，僅dry允許來源age **−100..10000ms**，並保存`source_time_profile=delayed_dry`及budget companion的來源診斷。Live搭配此旗標在讀settings／連線前拒絕；預設仍strict **0..3000ms**。Receipt≤3s、nonce/offset、來源時間單調、50ms clock-jump及帳戶／訂單／風險檢查不變，不重打時間戳。`outside_strict_source_packets`只計完整接受的封包，`outside_strict_source_checks`另含重驗；已接受age範圍及counter在close後仍保留。此選項只測延遲行情下的dry流程。

```powershell
.\.venv\Scripts\python.exe run_volume_market_maker.py --config config/market_maker_v2/test_local_quick.yaml --output logs/mm_v2_local_quick_new.jsonl --allow-delayed-dry-book
```

`test_local_quick.yaml`是ignored本機dry設定，duration=300；每次output使用新路徑。18個YAML欄位與size/risk不變，沒有新的服務或框架。這條CLI會保存JSONL和budget來源診斷；額外public BBO／loop／資源量測僅為本輪TEMP診斷。

## R4 離線報表

`FillEvent.source_timestamp_ms`是可選、非負整數的exchange來源時間；Lighter provider從原始trade保存，不由ingestion time推算UTC。舊JSONL缺欄位仍可讀，markout為unavailable；原`observed_monotonic`繼續控制ledger ingestion順序，governor另用保守cash-proof hold age。Runtime對帳失敗及缺完整final proof的窗口仍保存數值，但不能發布aggregate economics。

```powershell
.\.venv\Scripts\python.exe scripts/analyze_mm_v2_session.py logs/mm_v2_phase7_20260905/r3_t3_01.jsonl --candidate dry_t3_01 --mode dry_run --planned-seconds 1800 --wall-seconds 637.028 --market-observations logs/mm_v2_phase7_20260905/r3_t3_01.market.jsonl
```

Mode、預定窗口、whole-process時間及allocated capital是操作者提供的歷史宣告，工具不重新authenticate。Failed／early stop保留完整預定分母，overrun則延長分母；candidate使用所有窗口總分子除總分母，不平均單場ratio。Replay／dry不變成actual成交，不能把短暫有quote的時間替代wall window。Live標籤也不等於策略promotion。

公開book JSONL採`mm_v2_public_book_v1`，必須保留symbol、原始receipt monotonic、source timestamp(ms)、nonce、bid/ask及兩側size，價格／數量為Decimal字串；拒絕receipt/source/nonce倒退、相同receipt衝突及locked/crossed book。接收時間1s／5s mid return取horizon後第一筆、最多晚0.25s；成交markout獨立以maker fill source timestamp配對public source time，回signed未扣fee、turnover-weighted bps與coverage。Taker不混入maker markout。這些raw public BBO沒有own-order removal，亦沒有public trades／queue-position證據，不能當fill機率。Order lifetime以first-seen→first-absent execution snapshots估計，保留right-censored及資料缺口，dry/replay明示simulated。External quote distance初篩沿用`mm_v2_feasibility.py`的baseline及其不同配對規則。

Candidate table使用實際policy/governor及runner minimum-notional filter；所有financial inputs明示，entry=mid、zero age／loss／volatility是算術假設，不能替代margin／API／liquidity proof。以下僅重現schema example在歷史BBO及已核對minimum metadata下的數量表，50為假設配置資金，未改任何config：

```powershell
.\.venv\Scripts\python.exe scripts/analyze_mm_v2_session.py --candidate example_arithmetic --config config/market_maker_v2/lighter_btc_volume.example.yaml --external-bid 79691.3 --external-ask 79692.0 --tick-size 0.1 --size-step 0.00001 --min-order-size 0.00020 --min-notional 10 --maker-fee-rate 0.00012 --taker-fee-rate 0.00035 --allocated-capital 50 --target-edges 0 0.2 0.5
```

2026-09-06成交發現修正：total_trades_count只作統計提示，不再用未變counter阻止讀trades。精確cash/position/entry變化或原有一次retry會查100筆REST history；terminal proof若合法filled量尚未入帳，也走同一次retry。Financial fingerprint排除order counts、mark PnL及cash reuse開關；classic cash取raw.collateral，Unified取完整margin balance。已驗證但尚未反映在counter的fill數保留為ahead；counter追上只扣ahead、不重記fill；counter超前但缺history、下降或窗口不足仍拒絕。沒有額外常態輪詢、重試次數或API預留增加。

成交可發生在OM sync後、account read之前。正常報價授權若發現已知order exposure變化或known slot缺終態（orders=None），在同10s內消費一次account的orders handoff，重新同步並完成account/terminal proof後才重算風險。Execution port初始sync遇同類暫停，也可在原deadline內多同步一次，捕捉post-only rejection並須恢復HEALTHY後才取fresh quote。兩處及退出共用`can_reconcile_known_orders`：無local wire ambiguity latch、無unknown orders、無unresolved submit/cancel且所有slot ID均known；不能清除真正不明的wire結果。每次normal read照既有API admission，不借退出预留。Execution port只接受原已知order減量或exact terminal移除；新增、加量、換價、side/reduce-only變化仍阻斷。更新execution後才建立retained quotes，不使用舊plan；bounded exit原deadline/attempts/預留不變。

FailureDiagnostic可另帶whitelist Decimal scalars（cash/collateral/unrealized/total/cross、ledger/account position、stream/history counts），仍拒絕identity/token/raw payload；cash mismatch與valuation mismatch分開定位。API拒絕另保留當下`api_used_rest/ws/tx`及`api_next_rest/ws/tx`，避免以退出後peak推測拒絕瞬間。

2026-09-06 18:27 API持續運行修正：cash cache僅在cached positions全zero時嘗試重用；nonflat直接fresh cash audit，避免正常mark PnL變動觸發一次無益的valuation-cache race及forced trades。Active API budget的實盤正常cycle為5s，保留既有source/account時效，stop可即時喚醒。成功bounded risk exit並驗證flat/empty後，才能在`api_cooldown`等待重新進場餘裕；REST6000/WS32/TX4是headroom目標，不是完整週期成本上界，每次read/mutation仍照原normal admission。等待不送單，不重置ledger/損失/session deadline；恢復後需fresh sync/account/risk，stop/deadline則直接做最終authenticated核對。未知狀態、持倉或退出失敗不走此空倉等待，原退出限額與硬拒絕保留。

2026-09-06 18:45補齊正常API背壓：`ApiBudgetUnavailable`由account、normal quote以及已terminal cancel後proof保持原類型，不能包成資料錯誤或一般BLOCKED。Normal loop僅在budgetactive、非退出中且所有wire/known ownership可核對時執行同一次有界收尾；成功0/0後沿用上述空倉等待與原session deadline再入場。未知mutation或exit失敗不能恢復，也不重新給該次exit attempts。Expected backpressure只增加budget sidecar的`deferrals`，不產生FailureDiagnostic；真正故障、server429及無法完成cleanup仍診斷並失敗。Reserve只是admission檢查而非再次記入usage，保留原scheduled exit公式。驗收必須包含跨多個rolling窗口的持續maker/reprice/arrival race及成功重入；不能將budget碰線後安全停止視為可持續運行PASS。
