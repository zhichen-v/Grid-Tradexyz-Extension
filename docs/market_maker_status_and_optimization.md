# Market Maker 現況與優化方向

> 更新日期：2026-08-25。本文是簡要總覽；詳細證據、帳戶即時狀態與 GO／NO-GO 歷史，以 [Market Maker MVP Operating Guide](market_maker_mvp_operating_guide.md) 第 12 節及 fresh authenticated preflight 為準。

## 目前搭建現況

Market Maker MVP 已與 Grid runtime 分離，入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`。目前支援：

- Lighter 單一永續合約、每側最多一張 managed order。
- 雙邊或單邊 `POST_ONLY` 報價、fixed spread、fee-plus-buffer floor 與 inventory skew。
- 以持倉、live／uncertain orders 計算 worst-case exposure；到達風險區時停用增加曝險的一側並使用 reduce-only 報價。
- Raw spread、stale book／position、unknown order、ambiguous mutation、交易所或 reconciliation 異常時 fail closed。
- MM boundary不再信任Lighter book陣列首項：只保留finite且正數的price/size，以bid最大值／ask最小值重建BBO；target-symbol invalid book立即清除舊snapshot並fail closed。修復不在shared Lighter adapter或Grid production code內。
- Cancel-before-replace、mutation limiter、WS／REST 同步、dry-run、結構化 counters 與 shutdown cancel。
- In-process authenticated account audit：account-wide 掛單／持倉、session drawdown、unique BTC maker fills、exact fee、flat-to-flat turnover/net 與 wall／eligible-quote 成交量時速；managed orders 明確簽 integrator fee `0`，API `null` 只在已知訂單與本地零費率簽署證明同時存在時接受。Monitor worker failure 直接走主 coordinator 的 stop／shutdown-cancel；live 入口不允許停用 audit。
- Fake／mock 單元及整合測試、authenticated pre／postflight；成交稽核直接使用 authenticated trade endpoint，不再用 runtime fill-event counter 代替 ledger。

Market Maker 使用 shared Lighter adapter，但沒有併入 Grid strategy；後續仍應避免修改 Grid production code。若必須修改 shared adapter，需同時評估 Grid regression。

## 驗證狀態

- Step A–C 已驗證 read-only、單邊及雙邊極小額 quote lifecycle。
- Step D 兩次提前停止的歷史 **NO-GO** 不被改寫；第 12.19 節4小時live只代表pre-BBO-fix fingerprint的runtime／account-audit／cleanup歷史GO，不是現行parser的rollout gate。
- 現行MM-only BBO diff已完成第12.20節30分鐘exact-config T3：`354/354` cycles、184/184 samples `active`且raw spread `<=30 bps`、eligible `1840.171/1843.031s`；真實mutation／fills與所有fault counters為`0`，software/safety為 **GO**，live/economic仍 **NOT TESTED**。
- Step E 的 fee truth與 in-process audit已完成；4 小時 live為 `2769/2769` successful cycles，account audit持續 healthy，所有 uncertainty／reconciliation failure／unknown／429／WS reconnect為 `0`，clean shutdown後掛單為 `0`。
- E1 首次 live 取得兩筆自然 maker fills，沒有 taker fill，且 shutdown 後掛單為零；但 monitor 的跨 endpoint 判定有誤，hard stop 也未即時傳遞至主程序，因此 safety 為 **NO-GO**。
- 該輪未達至少 8 筆成交且未 flat-to-flat，economic result 為 **INCONCLUSIVE**，尚不能證明已 cover fee，也尚未開始 E2。
- 上述 monitor invariant／stop propagation、strict account parsing與 integrator zero-signing/null verification已取得本輪 exchange-path與 cleanup live proof；但只有 `3` 筆 maker fills、`0` completed fills／round trips且最終 nonflat，economic／fee-cover仍為 **INCONCLUSIVE**。
- 舊4小時live的turnover `46.993860 USDG / 3 maker fills`與economic `INCONCLUSIVE`仍有效；但`45.063s eligible`、`273 bps`及「市場本身長時間寬spread」是legacy first-level parser衍生值，不再作成交量或市場流動性基準。
- 2026-08-25 01:31兩次postflight快照為orders `0`、positions `0`、USDG total/free `299.752078 / 299.752078`；任何後續作業仍須fresh authenticated verification，不能把文件快照當成即時帳戶狀態。

目前仍是本地 MVP 驗證階段，不應視為 production-ready、持續獲利已證明，或 VPS rollout 已完成。

## 優化優先序

整體目標固定為：安全與無自成交 > cover 實際費用 > 放大 maker turnover／eligible quote hour > 淨盈利。

| 優先級 | 方向 | 完成條件 |
|---|---|---|
| P0 | 修正 monitor 的 account-wide invariant、bounded retry、liveness 與 monitor → main stop propagation | Deterministic tests、30m T3、4h live exchange path與 final cleanup已 GO；剩餘證據是達門檻的 completed economic gate |
| P1 | 將 unique fills、maker/taker、turnover、exact fee、net、wall/eligible hour 與 flat-to-flat equity reconciliation 自動化 | Runtime gate 已完成；exact fee 邊界包含 managed-order integrator explicit-zero proof；達 fill 門檻後 completed fee/net 立即判定，nonflat 時只延後 equity gate；funding／transfer 仍以 flat equity 差額診斷，正式 session GO 需 authenticated cashflow reconciliation |
| P2 | 先重建可信 E1 baseline，再一次只縮一項 spread；其後才調 inventory skew、reprice、lifetime 或 refresh | 全程高於 fee-plus-buffer floor、只含 maker fills、`gross >= exact fee`，且 completed net／turnover與 flat account-value change／turnover都 `>= 0.10 bps`；E2 起才要求 turnover／eligible hour 提升 |
| P3 | 完成長時間穩定性、log/audit retention 與資源監看；固定模型穩定後才評估 adaptive spread 或 order-book signals | pre-fix 4h runtime／資源平台與current-diff 30m T3已GO；剩餘工作是修復後live長時間樣本、自然flat economic sample與VPS另立階段 |

提高 order size、position cap、leverage、loss ceiling或使用 IOC，不是現階段預設優化手段，必須另行明確授權。

## 下一個安全順序

1. 現行BBO fingerprint的T3已GO；下一個live前仍須fresh authenticated驗證position／account orders均為`0`、maker fee仍為`1.2 bps`、BTC為`1x cross`，並取得新的明確live授權。
2. 以同一`250 ticks` candidate重建E1 live baseline；先觀察自然maker fills、eligible比例與account audit，不同時改spread、size、position cap或leverage。
3. 下一個live session仍須至少`8` completed maker fills、`gross >= exact fee`、completed `net / turnover >= 0.10 bps`，並在自然flat後通過account-value reconciliation；樣本不足不得宣稱GO。
4. E1 safety與economic同時GO且至少`10`個completed round trips後，才一次只調一項地評估`250 → 200 ticks`；不得以舊parser的低eligible結論或本次dry-run的高eligible直接推導成交量／盈利。VPS另立階段。

## Long-run 候選（2026-08-25）

本機 `config/market_maker/test_lighter_btc_mvp.yaml` 維持 `both / 0.00020 / max position 0.00040 / configured half-spread 250 ticks / 1.2 bps maker fee / 0.5 bps quote buffer`，並以 `15s` 帳戶稽核、`0.50 USDG` session drawdown、至少 `8` 筆 completed maker fills及 completed `net / Σabs(trade value) >= 0.10 bps`作為 gate。現行BBO修復fingerprint已完成`1843s` exact-config dry-run T3，software/safety為 **GO**；舊fingerprint另有約`4h00m` live runtime歷史，但其parser衍生的eligible/spread結論已失效，且live economic／fee-cover仍為 **INCONCLUSIVE**。

設定檔與已驗證 SHA-256仍保持 `dry_run: true`／`B8FD57C29BBD7A51A54B6769647F9F74A8CFA62BECD97F8BE993902FF7419475`；相同YAML不代表相同runtime fingerprint。現行程式與T3證據見operating guide第12.20節，舊4小時live事實見第12.19節。下次live前仍須fresh flat preflight與明確授權；不能因本次dry-run eligible約`99.84%`就直接縮spread、提高風險或宣稱成交量／fee cover已證明。
