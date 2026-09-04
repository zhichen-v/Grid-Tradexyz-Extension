# Market Maker V1 — Frozen entry point

> **LEGACY / FROZEN（2026-09-04）**。這不是目前候選或 live 啟動指南。過時參數、啟動命令與舊 promotion checklist 已移除，避免被誤用。

目前工作只有 [Volume-first V2 重構計畫](CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)，摘要見 [V2 objective](mm_v2/OBJECTIVE.md)。V2 尚未成為 production 推薦；V1 runtime/config/tests 仍保留，不代表授權繼續測試。

V1 歷史僅需以下入口：

- [最終 No-Go 摘要](market_maker_status_and_optimization.md)
- [凍結操作規則與事故原始紀錄](market_maker_mvp_operating_guide.md)
- [驗證歷史索引](market_maker_mvp_validation_history.md)

被精簡的原文可由本機 tag `mm-v1-guard-driven-20260903`（commit `59f313e7cc03c423877baa4b71e5467c67998e38`）恢復，例如：

```powershell
git show mm-v1-guard-driven-20260903:docs/market_maker_current_build_and_quickstart.md
```
