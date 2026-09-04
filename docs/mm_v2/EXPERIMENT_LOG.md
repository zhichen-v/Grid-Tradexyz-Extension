# Market Maker V2 — Experiment log

唯一 phase/run 記錄；不另建 status、campaign、checkpoint 報告。規格見 [rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)，產品與架構見 [OBJECTIVE](OBJECTIVE.md)、[ARCHITECTURE](ARCHITECTURE.md)。歷史帳戶讀值不代表現況。

| date | commit | config hash | duration | turnover/h | fee cover | net cost bps | DD | forced flatten | result |
|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04 | worktree on `59f313e7` | N/A | N/A | N/A | N/A | N/A | N/A | 0 | Phase 0 完成：本機 V1 tag + V2 branch；舊 docs 精簡／frozen；runtime、shared adapter、Grid 未改；無新帳戶讀取／mutation，未 commit/push。 |
| 2026-09-04 | worktree on `59f313e7` | N/A | offline | N/A | N/A | N/A | N/A | 0 | Phase 1 工具完成，economics 未評估：[feasibility CLI](../../scripts/mm_v2_feasibility.py) 347 LOC / 14,953 bytes；config fields 0，runtime LOC +0；focused/V2 23 PASS，legacy MM 650 PASS，full repo 920（既有 Grid/Lighter 8F+4E）；py_compile、diff-check、文件連結檢查通過。Historical Gate B fee + shadow BBO 184 筆，tick=0.1 為明確輸入假設；maker/taker 1.2/3.5 bps，fee floor 2.4 bps；edges 0/0.2/0.5 的 fullspread 2.4/2.6/2.9 bps、touch distance median 56/64/76 ticks（非 live 建議）。1s 無配對；5s 12/183（6.56%），不足推論代表性或 touch/fill frequency。下一步 Phase 2 isolated skeleton/safety ports；canary 前仍需密集唯讀 BBO／fresh fee 與逐場授權。無連線／account read／mutation，未 commit/push。 |
