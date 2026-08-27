# Market Maker 現況與優化方向

> 更新：2026-08-28 00:10（Asia/Taipei）。實際啟停與硬閘門以 [操作指南](market_maker_mvp_operating_guide.md) 及 fresh authenticated reads 為準。

## 現況

Market Maker 入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`，與 Grid runtime 分離。本輪未修改 Grid production code；shared Lighter adapter只新增預設關閉的MM pre-send opt-in，以及MM使用的read-only／exact-terminal hooks，Grid預設行為不變。

已具備：

- 單一 Lighter 永續合約、每側最多一張 managed order、單／雙向 `POST_ONLY` 報價。
- 正規化 external BBO、fixed spread、inventory skew、fee-aware exit、趨勢入場 guard 與有界 session-loss maker exit。
- Worst-case exposure、position cap、reduce-only收斂、oversized reduce-only dust cleanup。
- 精確 fill generation：fill與純 position refresh 分離，REST／WS／immediate-create replay不重複失效經濟快照。
- RiskManager 是 inventory age與soft-exit latch唯一來源，data/error recovery不重啟計時。
- Authenticated account audit：唯一maker fills、exact fee、flat-to-flat net/equity、fee tier、`1x cross`與帳戶獨占。
- Stale/untrusted data、unknown order、uncertain mutation、reconciliation failure與drawdown超限時 fail closed。

現行候選固定為：

`both / 250 ticks / order 0.00020 / max position 0.00040 / trend 60s/125 ticks / maker-exit loss 0.10 USDG / 1x cross / max drawdown 0.50 USDG / 8 mutations per minute`

Fee gate：maker `1.2 bps`、至少 `30 completed maker fills`、fee cover `>=1`、completed與自然flat equity皆 `>=+0.02 bps`。Source YAML維持 `dry_run: true`；ignored live copy只能改成 `dry_run: false`。

## 最新判定

- 21:41:59–21:47:37固定候選短live完成`12 completed / 5 round trips`，turnover`190.098420`、exact fee`0.02281181040`、net`+0.00500818960`、completed／flat-equity`+0.263452 / +0.263442 bps`、cover`1.21954`、max DD`0.023630`；約`127 completed fills/h`，成交量與fee cover都出現明顯正訊號。
- 本場仍是 **hard-safety NO-GO**：21:47:29 cancellation terminal proof未在既有有界窗口完成，current unresolved升為`1`，並短暫出現strategy position為flat但authenticated ledger仍short`0.00020`。依gate立即graceful stop；shutdown exact sync後ambiguity／resolved／unresolved為`1/1/0`、自然flat，但已有`1 failed cycle`且只到12 completed，不能倒推改判或進long-run。
- Cancel-vs-fill缺口已離線修復：只有MM bootstrap啟用exact outcome side-channel；同周期exact `FILLED`會保留成交資料、refresh position、阻止立即補單，且不誤增cancel success或resolved ambiguity。Status／side／ID alias衝突、無法確認ownership或沒有exact proof時仍保留uncertainty並fail closed；Grid預設不啟用cache。
- 新 guard 只用 external-BBO rolling mid：上漲時擋新增／加碼short，下降時擋新增／加碼long；reduce-only永不被擋，方向式50% hysteresis可解除或直接反轉。
- `max_session_loss_for_maker_exit: "0.10"`只供已鎖定的`POST_ONLY + reduce_only`退出使用。退出價格採 exact trade P&L 與最後同 generation 的 authenticated flat-equity P&L較差者；缺失／過期證據退回嚴格fee-aware價格，超過budget即hard stop。`bounded_economic_recovery`不是GO，原fee cover／completed／flat-equity門檻完全不變。
- Offline：最新精準`7/7`、Lighter cancellation`31/31`、Grid targeted`6/6`、Market Maker `319/319`；full repo `566`仍只有既知Grid baseline `8 failures + 4 errors`。Grid production未修改。完整13-file runtime fingerprint `200159C7F983A09113CF8B83148E76E398B7508367BBD104BCFA227E7701ABCE`；先前12-file值只漏列已存在的shared exception檔，沒有code drift。
- 20:55:38–21:11:33最終T3為`182/182` cycles、`would_place=243`；實際看到rising／neutral／falling多次切換。真實create/cancel、failed、ambiguity／unresolved、reconciliation failure、unknown、mutation blocks、429、WS與account-read failure皆`0`，全程flat。短live停止後process0，雙authenticated postflight亦為position／orders`0/0`，source config已byte-for-byte恢復`dry_run:true`。
- Cancel-race修復後T3於22:31:15–23:02:40完成：`360/360` cycles、`would_place=404`，真實mutation與全部hard-safety counters皆`0`，全程flat。Graceful stop後runtime0，雙authenticated postflight為position／orders`0/0`；證據`logs/market_maker_t3_cancel_race_20260827-230233.log`。
- 23:09:02–00:09:28固定候選短live為 **runtime/safety GO、volume/economic NO-GO**：`735/735` cycles、21 completed／10 round trips、turnover`322.806772`、exact fee`0.03873681264`、gross`-0.001964`、net`-0.04070081264`，completed／flat-equity約`-1.260841 / -1.260816 bps`，max DD`0.138115`。Safety counters均為0，兩次瞬時authenticated read failure由bounded retry吸收。
- 本場一度short`0.00030`並長時間維持`soft_exit_hard_fallback`的fee-preserving reduce-only bid；其後自然flat，但後段成交把先前正收益反轉為session loss`0.04070081264`。這證明loss budget有界，卻沒有證明fee cover或成交效率，不能進long-run。

## 下一步

1. 00:09停止後兩次authenticated postflight皆position0、open orders0；runtime0，source與fresh live copy都已恢復`dry_run:true`。
2. Cancellation修復的安全路徑已GO；目前真正blocker改為`soft_exit_hard_fallback`的證據／定價路徑，以及有界loss exit造成的fee-cover反轉。先做離線鑑識，不直接重跑。
3. Promotion條件不變：30 completed、自然flat、fee cover與completed／flat-equity雙門檻全部通過後，才能開始long-run。本場未達30且經濟明確NO-GO，禁止晉級。

不以 taker／IOC、self-trade或隱藏未完成episode換取成交量。VPS仍不在本階段。
