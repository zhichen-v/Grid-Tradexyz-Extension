# Market Maker 現況與優化方向

> 更新日期：2026-08-24（live 證據截至 2026-08-23；offline 修復與驗證截至 2026-08-24）。本文是簡要總覽；詳細證據、帳戶即時狀態與 GO／NO-GO 歷史，以 [Market Maker MVP Operating Guide](market_maker_mvp_operating_guide.md) 第 12 節及 fresh authenticated preflight 為準。

## 目前搭建現況

Market Maker MVP 已與 Grid runtime 分離，入口為 `run_market_maker.py`，核心位於 `core/services/market_maker/`。目前支援：

- Lighter 單一永續合約、每側最多一張 managed order。
- 雙邊或單邊 `POST_ONLY` 報價、fixed spread、fee-plus-buffer floor 與 inventory skew。
- 以持倉、live／uncertain orders 計算 worst-case exposure；到達風險區時停用增加曝險的一側並使用 reduce-only 報價。
- Raw spread、stale book／position、unknown order、ambiguous mutation、交易所或 reconciliation 異常時 fail closed。
- Cancel-before-replace、mutation limiter、WS／REST 同步、dry-run、結構化 counters 與 shutdown cancel。
- In-process authenticated account audit：account-wide 掛單／持倉、session drawdown、unique BTC maker fills、exact fee、flat-to-flat turnover/net 與 wall／eligible-quote 成交量時速；managed orders 明確簽 integrator fee `0`，API `null` 只在已知訂單與本地零費率簽署證明同時存在時接受。Monitor worker failure 直接走主 coordinator 的 stop／shutdown-cancel；live 入口不允許停用 audit。
- Fake／mock 單元及整合測試、authenticated pre／postflight；成交稽核直接使用 authenticated trade endpoint，不再用 runtime fill-event counter 代替 ledger。

Market Maker 使用 shared Lighter adapter，但沒有併入 Grid strategy；後續仍應避免修改 Grid production code。若必須修改 shared adapter，需同時評估 Grid regression。

## 驗證狀態

- Step A–C 已驗證 read-only、單邊及雙邊極小額 quote lifecycle。
- Step D 兩次 live 均提前停止，歷史判定仍為 **NO-GO**；先前 fingerprint 的事故修復與 T3 dry-run 曾通過，但不取代目前 long-run 候選的 fresh T3。
- Step E 的 fee truth 校準完成；2026-08-24 的 monitor／adapter／stop 修復候選已取得新一輪完整 T3 software/safety **GO**，但仍尚未以 live 成交證明 cover fee。
- E1 首次 live 取得兩筆自然 maker fills，沒有 taker fill，且 shutdown 後掛單為零；但 monitor 的跨 endpoint 判定有誤，hard stop 也未即時傳遞至主程序，因此 safety 為 **NO-GO**。
- 該輪未達至少 8 筆成交且未 flat-to-flat，economic result 為 **INCONCLUSIVE**，尚不能證明已 cover fee，也尚未開始 E2。
- 上述 monitor invariant／stop propagation，以及 account identity／equity／1x-cross strict parsing、audit timeout、shutdown safety-priority reads、partial-fill crossing、late terminal ID 歸因與 integrator zero-signing/null verification，已在 2026-08-24 完成程式修復與 deterministic tests；尚未取得新一輪 live proof，因此歷史 NO-GO 不變。
- 歷史 `SHORT 0.00040 BTC` 已由操作者處置；任何後續作業仍必須重新 authenticated verification，不能把文件快照當成目前帳戶狀態。

目前仍是本地 MVP 驗證階段，不應視為 production-ready、持續獲利已證明，或 VPS rollout 已完成。

## 優化優先序

整體目標固定為：安全與無自成交 > cover 實際費用 > 放大 maker turnover／eligible quote hour > 淨盈利。

| 優先級 | 方向 | 完成條件 |
|---|---|---|
| P0 | 修正 monitor 的 account-wide invariant、bounded retry、liveness 與 monitor → main stop propagation | Deterministic tests 與完整 30m T3 已 GO；剩餘證據是受監控 live canary/long-run 中的 exchange path、成交後經濟 gate 與最終 cleanup |
| P1 | 將 unique fills、maker/taker、turnover、exact fee、net、wall/eligible hour 與 flat-to-flat equity reconciliation 自動化 | Runtime gate 已完成；exact fee 邊界包含 managed-order integrator explicit-zero proof；達 fill 門檻後 completed fee/net 立即判定，nonflat 時只延後 equity gate；funding／transfer 仍以 flat equity 差額診斷，正式 session GO 需 authenticated cashflow reconciliation |
| P2 | 先重建可信 E1 baseline，再一次只縮一項 spread；其後才調 inventory skew、reprice、lifetime 或 refresh | 全程高於 fee-plus-buffer floor、只含 maker fills、`gross >= exact fee`，且 completed net／turnover與 flat account-value change／turnover都 `>= 0.10 bps`；E2 起才要求 turnover／eligible hour 提升 |
| P3 | 完成 Step D 長時間穩定性、log/audit retention 與資源監看；固定模型穩定後才評估 adaptive spread 或 order-book signals | 無 unresolved uncertainty、429 storm或資源成長；inventory drift 在風控內且可自然回復，VPS 另立階段 |

提高 order size、position cap、leverage、loss ceiling或使用 IOC，不是現階段預設優化手段，必須另行明確授權。

## 下一個安全順序

1. Fresh authenticated preflight 驗證 BTC position／account orders 均為 `0`、費率仍為 maker `1.2 bps`、BTC 為 `1x cross`。
2. 目前 fingerprint 的 30m exact-config T3 已 GO；YAML SHA-256 為 `B8FD57C29BBD7A51A54B6769647F9F74A8CFA62BECD97F8BE993902FF7419475`。
3. 以同一 E1 `250 ticks` 設定啟動 live；前 `15–30m` 當 safety canary，健康即讓同一 session 繼續，不在 canary 與 long-run 間重設 drawdown baseline。
4. 累積至少 8 筆 completed maker fills後，已封口 episodes 的 `gross >= exact fee` 與 completed `net / turnover >= 0.10 bps` 立即判定；通過但 nonflat 時為 `fee_gate_pass_equity_pending_flat`，自然回到 flat後才判定 account-value change／turnover `>= 0.10 bps`。樣本不足時維持 collecting／incomplete，不假稱 GO。
5. 長測持續記錄 `maker_turnover_per_wall_hour`、`maker_turnover_per_eligible_hour`、對應 fill/hour、drawdown 與 flat equity discrepancy；任何 hard stop 後不得自動重啟。最終停止後才驗證 cleanup/postflight。
6. E1 safety 與 economic 同時 GO，且至少取得 `10` 個 completed round trips與 funding／transfer postflight核對後，才以一次只改 spread 的方式評估 `250 → 200 ticks`；不得同時提高 size／position cap。

## Long-run 候選（2026-08-24）

本機 `config/market_maker/test_lighter_btc_mvp.yaml` 維持 `both / 0.00020 / max position 0.00040 / configured half-spread 250 ticks / 1.2 bps maker fee / 0.5 bps quote buffer`，新增 `15s` 帳戶稽核、Step E 較嚴格的 `0.50 USDG` session drawdown、至少 `8` 筆完成 maker fills，以及 completed `net / Σabs(trade value) >= 0.10 bps`。後者等於 `0.001%`，是 flat-to-flat 累積門檻，不是開倉單或單筆 fill 的門檻；runtime 亦要求同口徑的 flat account-value change 通過。這個最終候選已於 2026-08-24 完成 `1872s` dry-run T3，software/safety gate 為 **GO**。

設定檔仍保持 `dry_run: true`。操作者開始 live 前只應重新確認 wallet profile、authenticated fee tick、flat／orders `0`、`1x cross` 與沒有其他 account writer，然後在執行副本明確將 `dry_run` 改為 `false`；其餘參數先不要改。Runner 沒有自動 duration，前 `15–30m` 須由操作者視為 canary 並在異常時 Ctrl+C，健康才繼續 long-run。若要提高成交量，必須先取得本候選 E1 GO，下一階段才單獨把 half-spread 改為 `200`，讓 eligible-quote turnover/hour 有可比較基準；完整命令與停止條件見 operating guide 第 12.17 節。
