# Market Maker 現況與優化方向

> 更新：2026-08-30 active-unwind truth-lifecycle修復後60分鐘live canary closure（Asia/Taipei）。實際啟停與硬閘門以 [操作指南](market_maker_mvp_operating_guide.md) 及 fresh authenticated reads 為準。

## 現況

Market Maker 入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`，與 Grid runtime 分離。本輪未修改 Grid production code；shared Lighter adapter只新增預設關閉的MM pre-send opt-in，以及MM使用的read-only／exact-terminal hooks，Grid預設行為不變。

舊候選的控制面可穩定運行：單一mutation authority、ownership、uncertainty／reconciliation、account audit、shutdown與stranded guard均能按規則收斂或fail closed。缺口在**單邊行情下的inventory lifecycle**，不是程序穩定性：例如先成交short後價格持續上漲，既有fee/equity hard gate可能讓reduce-only maker quote長期停在normal band外；guard只能安全撤單停機，無法在同一episode內動態追價並有界地主動減倉。

已具備：

- 單一 Lighter 永續合約、每側最多一張 managed order、單／雙向 `POST_ONLY` 報價。
- 正規化 external BBO、fixed spread、inventory skew、fee-aware exit、趨勢入場 guard 與有界 session-loss maker exit。
- 可選的 position-based ping-pong：持倉後只留反向 `POST_ONLY + reduce_only`，並等 authenticated ledger 與 fill generation 都確認 flat 才重開雙邊。
- Worst-case exposure、position cap、reduce-only收斂、oversized reduce-only dust cleanup。
- 精確 fill generation：fill與純 position refresh 分離，REST／WS／immediate-create replay不重複失效經濟快照。
- RiskManager 是 inventory age與soft-exit latch唯一來源，data/error recovery不重啟計時。
- Authenticated account audit：唯一maker fills、exact fee、flat-to-flat net/equity、fee tier、`1x cross`與帳戶獨占。
- Stale/untrusted data、unknown order、uncertain mutation、reconciliation failure與drawdown超限時 fail closed。
- `soft_exit_latched`後若正確方向的reduce-only實際報價超出reference的normal half-spread加1 tick，先fail-closed撤managed orders再fatal stop；不改strategy算價、economic gate或maker-only限制。
- 本次code-only加入per-position episode executor：嚴格fee-aware profit exit、soft-exit後passive chase／progressive loss unlock，以及deadline／loss trigger後的隔離active lane。Active lane僅允許`reduce_only LIMIT + IOC`，先撤managed orders並取得authenticated zero-orders proof，再刷新position／audit／BBO及一次性generation truth；slippage、episode／session loss、drawdown、attempt與confirmation timeout皆有界。

現行候選固定為：

`both / ping-pong / 250 ticks / order 0.00020 / max position 0.00040 / trend 60s/125 ticks / maker-exit loss budget 0 / active unwind OFF / 1x cross / max drawdown 0.50 USDG / 8 mutations per minute`

Fee gate：maker `1.2 bps`、至少 `30 completed maker fills`、fee cover `>=1`、completed與自然flat equity皆 `>=+0.02 bps`。Source YAML維持 `dry_run: true`；ignored live copy只能改成 `dry_run: false`。

Active unwind目前為 **default OFF / explicit opt-in / 修復後60分鐘live runtime-safety GO / IOC未觸發且live execution仍未證明**。`active_unwind_success`只代表某次active order取得乾淨terminal proof，可能是no-fill或partial；只有新的authenticated flat checkpoint才代表episode完成。此功能不會自動取得live資格，也不改變現行source的dry-run狀態。

## 最新判定

- 對使用者提出的單邊庫存問題，判斷仍是「控制面穩定，但退出生命週期需要獨立active lane」。Active-enabled dry T3已GO；首次live canary安全到達`active_ready`，卻因truth token被一般debounce吞掉而沒有送出IOC，因此不能宣稱active execution已live驗證。
- 16:31:00–17:01:00 live final為`387/387`、failed`0`、short`0.00020`、orders`0`，active attempts與全部hard-safety counters`0`。Evidence `logs/market_maker_active_unwind_live_active_ready_stall_final_20260830-170100.log`，SHA `E8C084F7...6FC1A`。
- 殘倉依授權只用單向`BUY LIMIT + POST_ONLY + reduce_only`於`78045.8`取得exact fill回到flat，未用IOC／market／taker。兩次authenticated postflight確認position／orders=`0/0`、used collateral=`0`。
- MM-only最小修復讓armed token只繞過一次normal debounce；token遺失但既有preparation generation仍有效時，先fresh BBO／REST position／account audit再re-arm，read／rearm失敗仍fail closed且不執行。MM`370/370`；full repo`617`維持既知Grid `8F+4E`。
- 17:18:44–17:51:06修復後fresh T3 final為`370/370`、`would_place=580`，position／orders=`0/0`、真實create/cancel`0`，全部failed／hard-safety／active counters`0`。Evidence `logs/market_maker_active_unwind_fix_t3_20260830-175102.log`，SHA `64DF92235D4A5BFD143575FA8F023960448C3FA81F92F938BD0E6121C0FE9754`；runtime0，雙postflight全零。
- 18:20:29–19:20:55修復後60分鐘live canary final為`756/756`、failed`0`、10 completed maker fills／5 round trips、final flat、orders與runtime`0`；全部hard-safety counters、taker fills及active attempts皆`0`。Turnover`156.112160`、gross`0.004240`、exact fee`0.01873345920`、net`-0.01449345920`、completed／flat-equity`-0.928400 / -0.928435 bps`、cover`0.226333`、max DD`0.016619`。正式判定為**runtime／hard-safety GO；economic collecting；active IOC path仍未證明**：10 fills未達30-fill gate，負bps不能提前判economic NO-GO；active未觸發也不能當IOC proof。Evidence `logs/market_maker_active_unwind_live_60m_final_20260830-192131.log`，SHA `74B673BC5E5BB3E01AD6AFCA04D437F6A2873EA4E3BB778B7EF7A19754C7B721`；雙authenticated postflight position／orders=`0/0`、used collateral=`0`、equity`299.264289`，live copy已恢復dry-run。

- Invalid-nonce definitive-rejection修復只接受精確`21104 / invalid nonce / {}`且需MM opt-in，hard-refresh nonce後最多下一cycle重試；其他錯誤仍fail closed。23:15:28–23:49:40 T3為`390/390`、真實mutation與全部hard-safety counters`0`，evidence `logs/market_maker_t3_invalid_nonce_20260829-234937.log`。
- 23:50:37–03:50:37首輪4小時雖為`2789/2789`且6 completed／3 round trips的completed net為`+0.0240908 bps`、cover`1.02008`，但少於30且final short`0.00020`，只能記`incomplete_nonflat`；依授權以單向`BUY LIMIT + POST_ONLY + reduce_only`於`78139.1` exact fill回到flat。
- 已完成stranded-soft-exit最小guard與deterministic mirror tests；MM`333/333`，full repo`580`維持既知Grid `8F+4E`。04:20:10–04:53:16 T3為`379/379`、真實mutation與hard-safety counters全`0`，evidence `logs/market_maker_t3_stranded_guard_20260830-045312.log`。
- 第一次live於05:49由guard按設計中止：`705/705`、failed與其他hard-safety counters`0`，short`0.00020`且orders已撤為`0`；maker-only recovery回到flat後，05:57:11–06:28:11 fresh T3 final再以`354/354` GO。Stop與T3 evidence分別為`logs/market_maker_long_run_stranded_stop_20260830-055234.log`、`logs/market_maker_t3_post_recovery_20260830-062753.log`。
- 第二次live於07:29:09決定性重現同一intended guard：`775/775`、1 maker fill／0 completed、economic state `incomplete_nonflat`、short`0.00020`、orders與runtime均`0`，全部hard-safety counters仍`0`。依授權只用單向`BUY LIMIT + POST_ONLY + reduce_only`在`78215.5`取得exact terminal fill；07:34兩次authenticated postflight均position／orders=`0/0`、used collateral=`0`、equity`299.299693`。同候選不再自動重跑；evidence `logs/market_maker_long_run_stranded_stop_repeat_20260830-072909.log`、`logs/market_maker_recovery_repeat_20260830-073412.txt`。
- 18:27 long-run依當時monitor規則停止，正式紀錄仍是Operational NO-GO／economic evidence unavailable；但鑑識已定位為觀測誤報：18:33:36.467 status讀到adapter剛登記的cancel proof marker，18:33:36.557同一cancel已由exact terminal history證明並清除，只有約90ms，runtime本身沒有failed cycle、unknown或reconciliation failure。
- 已做MM-only最小修復：公開`unresolved_cancellation_count`不再把受管slot正處於`CANCELING`且尚未轉uncertain的同一key當成current unresolved。真正uncertain、adapter-only／mismatched key仍計數；`has_uncertain_state`與實際fail-closed阻擋不變，shared Lighter/Grid未修改。OrderManager`88/88`、MM`327/327`通過；full repo`574`維持既知Grid `8F+4E`。
- 18:53:59–18:59:10以新fingerprint `7A3A8DB9...99F9DF`完成dry-run T1：final `60/60` cycles、真實create/cancel與全部hard-safety counters為`0`、全程flat。Runtime0，雙authenticated postflight position／orders=`0/0`；證據`logs/market_maker_t1_cancel_confirmation_metric_20260829-185910.log`。
- E2z的false unknown-order根因已做MM-only最小修復：terminal後延遲WS replay必須匹配side、order/client namespace、amount與price；remaining low-watermark只下降，新增partial只觸發一次refresh，同一replay不重複計fill。Foreign／衝突order及REST active-after-terminal仍fail closed；shared Lighter adapter與Grid均未修改。
- 本地候選只改一個經濟變因：`max_session_loss_for_maker_exit: "0"`，停用以固定session loss換成交，恢復既有fee／authenticated-surplus aware exit。Source仍`dry_run:true`，SHA`9162163C...84DF711`。
- Offline/regression與fresh T3均GO：terminal replay`4/4`、order manager`87/87`、經濟分支`4/4`、Market Maker`326/326`；full repo`573`維持既知Grid baseline `8 failures + 4 errors`。00:08:13–00:38:39 T3保存狀態`348/348`，真實mutation、failed、ambiguity／unresolved、reconciliation failure、unknown、429、WS與account-read failure皆`0`，全程flat。Graceful stop後runtime0，00:39兩次authenticated postflight均position／orders=`0/0`、used collateral=`0`；證據`logs/market_maker_t3_terminal_replay_20260829-003823.log`，本輪沒有live mutation。
- 00:44:19–01:04:29固定邊界短live完整通過經濟gate：`277/277` cycles、32 completed／16 round trips、turnover`498.137240`、exact fee`0.05977646880`、gross`0.071120`、net`+0.01134353120`、completed／flat-equity`+0.227719 / +0.227708 bps`、cover`1.18977`、max DD`0.011374`。Session-loss與bounded-recovery分支均未使用；soft exit只在authenticated completed surplus為正時生效。Failed、ambiguity／unresolved、reconciliation failure、unknown、429、WS與account hard stop皆`0`。
- 30 fills且authenticated flat時已是`fee_and_equity_gate_go`。其後一次position snapshot unavailable觸發fail-closed停止；account audit維持healthy，停止窗口中的maker episode自然完成，final audit為32 fills且flat。Runtime0，雙postflight position／orders=`0/0`、used collateral=`0`、equity`299.383977`，兩份config皆dry-run。Terminal replay未在此live自然出現，故該分支仍只有offline deterministic proof。
- Ping-pong正式T3已GO：20:16:00–20:46:25為`348/348` cycles，真實mutation與全部hard-safety counters為`0`，全程flat；graceful stop後runtime0、雙postflight `0/0`。
- 20:48:12–21:36:32短live已反覆證明ping-pong核心行為：持倉後不再同向加碼，只留反向`POST_ONLY + reduce_only`；exit fill後等待authenticated flat才開下一episode。因此原先`0.00030/0.00040`累積庫存問題已收斂。
- 本場仍為 **hard-safety／economic NO-GO**：34 completed／16 round trips，completed turnover`485.272502`、exact fee`0.05823270024`、gross`-0.009290`、net`-0.06752270024`、`-1.391439 bps`、cover`-0.15953`、max DD`0.078506`。21:34:02同一受管order在cancel terminal confirmation清slot後才收到延遲partial-fill／nonterminal WS update，被active-slot-only matching誤判unknown；counter`2`是同一事件的兩次觀察，不是外部單。10秒內REST exact sync已解除pause，仍依累計hard gate停止。
- 21:48依使用者授權，以既有單向helper完成唯一`POST_ONLY + reduce_only SELL 0.00020` recovery。Helper回報flat後正常退出；21:48:27與21:48:35兩次authenticated postflight皆position／orders=`0/0`、used collateral=`0`。Source與ignored live copy均維持`dry_run:true`。

## 下一步

1. 保持flat、runtime0、open orders0，所有source／本地候選config維持`dry_run:true`；active lane仍default off，不自動延長或重啟live。
2. 修復後MM `370/370`、fresh 30分鐘T3與60分鐘live runtime／hard-safety均GO；full repo `617`只保留既知Grid baseline `8F+4E`。Live場沒有active attempt，因此仍不證明IOC terminal path。
3. 任何`active attempts=0`的live都不能當作IOC terminal proof。後續若要驗證`active_ready → bounded IOC → exact terminal → authenticated flat`，仍需使用者新的明確live授權與fresh-flat preflight；不得為製造觸發而放寬threshold或其他risk gate。
4. 本場只有10 completed fills，economic state仍為`collecting`；雖然觀察值為負，也不得在30-fill gate前判economic NO-GO或據此promotion。後續live仍須維持symbol獨占、shutdown撤單、正值authenticated taker fee與全部deadline／loss／slippage／attempt／timeout cap，不得同時調`250 ticks`、size、margin或風險額度，也不得用market、self-trade或未完成episode換取成交量。

Active IOC是唯一、預設關閉且有界的taker例外，不是成交量工具。VPS仍不在本階段。
