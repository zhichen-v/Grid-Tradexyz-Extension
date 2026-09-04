# Market Maker V1 — Final status (frozen)

> **LEGACY / FROZEN（2026-09-04）**。舊「下一步優化」、Gate/toxicity/campaign 工作清單已失效並移除。唯一新任務為 [V2 重構計畫](CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)，不是繼續修補 V1。

## 最終結論

V1 未通過 production promotion。2026-09-03 的 30min maker-only shadow live 完成 `344/344` cycles、hard counters 為零，卻只有一筆 SELL entry，行情上漲後沒有完成退出。正常 runtime 不等於 inventory lifecycle 收斂；cancel-only shutdown 留下 short，使用者其後自行平倉。

當時配置為 `active_unwind_enabled:false`、`max_session_loss_for_maker_exit:0`；這組不虧損被動退出限制是 V1 問題的一部分，**不得沿用為 V2 stop-loss/flatten 政策**。不再以自然 flat、30 completed maker fills 或舊 Gate C 作新版本目標。

2026-09-03 23:47 兩次 authenticated postflight 的 position / orders / used collateral 為 `0 / 0 / 0`，flat equity `298.814557 USDG`；相對該場 baseline，觀測 loss 為 `0.265309 USDG`。這些是**歷史快照，不代表目前帳戶狀態**，舊風險預算與 live 授權亦不自動轉用至 V2。

## 證據與恢復

- 完整事故與測試 baseline：[凍結 V1 操作指南](market_maker_mvp_operating_guide.md)。
- 較早驗證：[歷史索引](market_maker_mvp_validation_history.md)。
- 本地 ignored 原始 evidence：`logs/market_maker_evidence/shadow_live_0a37798_20260903-2212.json`，SHA-256 `A0E6B51E7E96E6F2E422FCCEAD0F74E50A01AC9D6C490CD6DB6D74312A594503`；不更改、不重判為 GO、不提交 logs。
- 舊正文可用 `git show mm-v1-guard-driven-20260903:docs/market_maker_status_and_optimization.md` 讀取。新 phase/run 結果只放 [V2 EXPERIMENT_LOG](mm_v2/EXPERIMENT_LOG.md)，不再追加於本檔。
