# Market Maker 現況與優化方向

> 更新：2026-08-29 01:07（Asia/Taipei）。實際啟停與硬閘門以 [操作指南](market_maker_mvp_operating_guide.md) 及 fresh authenticated reads 為準。

## 現況

Market Maker 入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`，與 Grid runtime 分離。本輪未修改 Grid production code；shared Lighter adapter只新增預設關閉的MM pre-send opt-in，以及MM使用的read-only／exact-terminal hooks，Grid預設行為不變。

已具備：

- 單一 Lighter 永續合約、每側最多一張 managed order、單／雙向 `POST_ONLY` 報價。
- 正規化 external BBO、fixed spread、inventory skew、fee-aware exit、趨勢入場 guard 與有界 session-loss maker exit。
- 可選的 position-based ping-pong：持倉後只留反向 `POST_ONLY + reduce_only`，並等 authenticated ledger 與 fill generation 都確認 flat 才重開雙邊。
- Worst-case exposure、position cap、reduce-only收斂、oversized reduce-only dust cleanup。
- 精確 fill generation：fill與純 position refresh 分離，REST／WS／immediate-create replay不重複失效經濟快照。
- RiskManager 是 inventory age與soft-exit latch唯一來源，data/error recovery不重啟計時。
- Authenticated account audit：唯一maker fills、exact fee、flat-to-flat net/equity、fee tier、`1x cross`與帳戶獨占。
- Stale/untrusted data、unknown order、uncertain mutation、reconciliation failure與drawdown超限時 fail closed。

現行候選固定為：

`both / ping-pong / 250 ticks / order 0.00020 / max position 0.00040 / trend 60s/125 ticks / maker-exit loss budget 0 / 1x cross / max drawdown 0.50 USDG / 8 mutations per minute`

Fee gate：maker `1.2 bps`、至少 `30 completed maker fills`、fee cover `>=1`、completed與自然flat equity皆 `>=+0.02 bps`。Source YAML維持 `dry_run: true`；ignored live copy只能改成 `dry_run: false`。

## 最新判定

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

1. 固定`250 ticks`短場已達30 completed、自然flat與完整economic GO；不再自動重跑或同時改spread、size、風險額度。
2. 下一階段是同fingerprint的有界long-run，但必須取得新的明確授權並先做fresh authenticated preflight；本輪短live授權不延伸到long-run。
3. Terminal replay live path未自然觸發；保留offline deterministic coverage，不為製造證據而刻意觸發交易競態。
4. 若long-run後仍需比較`200 ticks`，維持單變因流程；不以loss-budget、taker或未完成episode換取成交量。

不以 taker／IOC、self-trade或隱藏未完成episode換取成交量。VPS仍不在本階段。
