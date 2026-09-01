# Market Maker 現況與優化方向

> 更新：2026-09-01 entry-reserve short-live gate（Asia/Taipei）。實際啟停與硬閘門以 [操作指南](market_maker_mvp_operating_guide.md) 及 fresh authenticated reads 為準。

## 現況

Market Maker 入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`，與 Grid runtime 分離。本輪未修改 Grid production code；shared Lighter adapter只修正IOC limit的SDK expiry mapping，POST_ONLY/GTT欄位與Grid預設行為不變，並以Grid POST_ONLY regression驗證。

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
- Live flat entry admission會保留完整`max_episode_loss_for_unwind`，並要求fresh authenticated session budget及drawdown headroom都足夠；stale／missing economics只阻擋新episode，不偽計cap event。Dry-run零mutation旁路只為保留T3可觀測性。
- Loss barrier每次trusted audit獨立於soft-exit latch計算，time barrier只依episode age；`episode < session < drawdown`為config hard invariant。Ignored active overlays目前為`0.075 < 0.10 < 0.50 USDG`且都維持`dry_run:true`。
- Live達最小樣本後若fee cover／completed net／flat-equity gate失敗，立即鎖新episode；nonflat只允許reduce-only退出，authenticated flat後`no_go`停止。`bounded_economic_recovery`只留給dry-run validation。
- Bounded episode ledger保存entry side、gross、maker／taker／exact fee、net、active involvement與final flatten lane；executor另保存episode ID、實際active attempts及loss／time trigger。Markout以WS與reconciliation的order-level增量fill記錄1／5／15／60秒、MAE／MFE及分側摘要。

現行候選固定為：

`both / ping-pong / 250 ticks / order 0.00020 / max position 0.00040 / trend 60s/125 ticks / maker-exit loss budget 0 / active unwind OFF / 1x cross / max drawdown 0.50 USDG / 8 mutations per minute`

Fee gate：maker `1.2 bps`、至少 `30 completed maker fills`、fee cover `>=1`、completed與自然flat equity皆 `>=+0.02 bps`。Source YAML維持 `dry_run: true`；ignored live copy只能改成 `dry_run: false`。

Active unwind目前為 **default OFF / explicit opt-in / execution path live-proven / entry-reserve prevention live-proven / production rollout未通過**。`OrderExpiry is invalid`已修復，歷史live已取得full、partial與residual IOC的exact terminal proof；2026-09-01 short-live則在authenticated flat且剩餘session budget不足完整episode reserve時，於新下單前正確block。這不會回溯改判舊場次，也不等於4h或production GO。`active_unwind_success`只代表某次active order取得乾淨terminal proof，可能是no-fill或partial；只有authenticated flat checkpoint才代表episode完成。

## 最新判定

- 02:24:45–02:47:56以P0／P1後active overlay執行最多60分鐘short-live gate。第8個flat-to-flat episode完成後，final `315/315` cycles、16 completed maker fills／8 trips、taker`0`；turnover`252.524840`、gross`-0.002040`、exact fee`0.03030298080`、net`-0.03234298080`、completed／flat-equity`-1.280784132 / -1.280784892 bps`、cover`-0.06732011`、max DD`0.035207`。02:47:25 authenticated flat時剩餘session unwind budget`0.067657 < 0.075` full-episode reserve，因此`entry_admission=blocked`、`episode_cap_blocked=1`，任何第9輪下單前即停止。這是 **P0 live prevention proof GO，但short-live promotion NO-GO**；不可藉重置session accounting或放寬`0.10` cap續跑，所以已授權的4h階段沒有啟動。16 fills未達正式30-fill gate，economic state仍是`collecting`；負值是adverse signal，不冒充正式30-fill NO-GO。
- Runtime failed、ambiguity／unresolved、reconciliation failure、unknown、429及全部active counters皆`0`。1次account read failure約10秒內恢復authenticated flat；1次account WS close約5秒重連並重訂閱；23次cancel均由adapter exact-terminal reconciliation收斂。BUY與SELL的1／5／15／60秒平均markout全部為負，side-specific controller仍保持停用。單次Ctrl+C後runtime0；02:52雙authenticated postflight皆position／orders=`0/0`、used collateral`0`、equity`299.069583`。兩份ignored config均恢復`dry_run:true`且同SHA`E60F3093...8C16D`。Evidence `logs/market_maker_short_live_entry_reserve_nogo_20260901-024756.log`，SHA`BC60B6C4...6F1109`；metrics SHA`057C868C...F81F79`。
- 2026-08-31已完成P0／P1與P2量測基礎的code-only hardening：full-stop entry reserve、pre-latch loss barrier、strict cap hierarchy、flat-only live economic stop、audit／cycle atomic stop latch、episode ledger／execution history、WS＋ordinary reconciliation＋REST sync增量markout，以及maker eligible-hour turnover修正。Focused`328/328`、MM`400/400`通過；full repo`647`維持既知Grid `8F+4E`。沒有exchange connection、live mutation、flatten或新account read；因此判定只到offline，尚不是dry safety或live GO。
- 21:36:56–22:07:38以active dry source完成fresh 30分鐘T3：final uptime`1844.766s`、`351/351` cycles、failed`0`、`would_place=388`，真實create／cancel、account read failure、reconciliation failure、unknown、ambiguity／unresolved、blocks、429、WS、active及episode-cap counters全`0`；全程flat，entry-admission dry-run旁路與markout三來源schema可見。單次Ctrl+C後runtime0，22:08兩次authenticated postflight皆position／orders=`0/0`、used collateral`0`、equity`299.101926`。判定為 **Dry safety GO；live仍未授權**。Raw evidence `logs/market_maker_t3_episode_cap_markout_20260831-220738.log`，SHA `762CB7B1...C1C152`；1000-line handler在uptime`574.641s`重置，故raw檔只保留後段，前段精確monitor checkpoints及限制另存同名metrics檔，不得稱完整start-to-stop transcript。
- Side-specific adverse-selection controller尚未啟用。先用fresh T3／短場收集足夠的BUY／SELL markout、MAE／MFE與episode net，確認coverage後才能一次只調一個side widening／pause門檻；不得直接縮spread、增加層數或提高mutation rate。
- 單邊行情處理**確實有動作，但不是無條件平倉**：持倉後先抑制同向加碼、用反向POST_ONLY減倉；逾時／虧損觸發時才進入有界active IOC。若預估損失超過episode／session cap，安全規則會拒絕送單並停機。因此「沒有成交」可能是maker價未到，也可能是cap在submission前阻擋；不能把這解讀成完整的production inventory manager。
- `OrderExpiry is invalid`根因是adapter未替IOC limit傳SDK要求的`DEFAULT_IOC_EXPIRY`。最小修復只改IOC mapping；無pre/post-send provenance的同字串仍保持ambiguous。Focused`3/3`、integration`33/33`、MM`371/371`、Grid POST_ONLY`3/3`通過；full repo`618`維持既知Grid `8F+4E`。
- 21:14:18–21:47:45 fresh T3為`383/383`、`would_place=537`，真實mutation與全部hard-safety counters`0`。Evidence `logs/market_maker_t3_ioc_expiry_20260830-214745.log`，SHA `FEF6B22B44F25D099FDFB0083F71F5E1185735C96939A2632BB646E8636A6BF5`；runtime0且雙preflight flat／orders0。
- 21:48:54 final live rerun啟動。22:22 checkpoint為`465/465`、9 maker／1 taker fills、5 trips、flat；首個active IOC full-fill取得exact proof。其後另一episode取得partial及residual IOC exact proof，累計active attempts／success／partial=`3/3/1`，active ambiguity`0`。
- 22:49:54第10輪long`0.00020`達active deadline，但新IOC會越過`max_session_loss_for_unwind=0.10`，剩餘budget僅`0.003245`，因此在submission前block並fatal stop。Final `832/832`、failed`0`、18 unique maker／2 taker、9 trips，全部ambiguity／unresolved／reconciliation failure／unknown／non-maker／429／WS／account hard counters`0`；3次account read failure與數次短暫stale pause均自行恢復，沒有持續data／account hard failure。Max DD`0.102764 < 0.50`。Evidence `logs/market_maker_final4h_session_loss_cap_stop_20260830-224954.log`，SHA `FEC3343ED3D703482BC9F0DDFA46DFD0A1A6369B545093D31BC66E8E8E7BBF20`。
- 這一輪證明active execution可用，也證明risk cap會安全阻擋；但只運行約61分鐘、17 completed maker fills且final nonflat，**不是4h完成、不是economic GO／NO-GO、也不是production promotion**。Runtime0、orders0；22:53雙authenticated postflight一致為BTC long`0.00020 @ 78707.9`、used collateral`15.741580`、equity`299.047812`。兩份config均已恢復`dry_run:true`，沒有新flatten授權，故未處置殘倉。

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

1. 保持source與兩份ignored active overlay為`dry_run:true`、active lane default off。最新02:52雙authenticated account evidence為BTC position／orders=`0/0`、used collateral`0`；這只證明該時點，不得假定狀態永久不變。
2. 目前固定候選不得進入4h。Entry reserve已證明會正確拒絕付不起完整stop的新episode；不得為累積30 fills或完成4h而重置本場session economics、提高`0.10` session cap或降低`0.075` episode reserve。
3. 先離線分析本場16筆雙側負markout與episode economics，一次只形成一個可驗證的新候選；在fee cover、completed net與flat-equity有合理改善依據前，不縮spread、不增加quote layer、不提高mutation。
4. 任何新候選都須先跑受影響tests、完整Market Maker suite與fresh T3；其後另取明確live授權，並在啟動前再次用雙authenticated preflight證明flat／orders0。舊場次不得拼接成新session或4h證據。

Active IOC是唯一、預設關閉且有界的taker例外，不是成交量工具。VPS仍不在本階段。
