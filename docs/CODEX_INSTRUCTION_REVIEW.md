# AGENTS 與 Skills 指令審閱 — 2026-09-06

本次依使用者要求，優化 GPT-6 Astra 執行任務時的自主性、澄清、批准與完成條件。設計原則是明確交付成果、風險邊界與驗收證據，讓模型自行選擇方法；不靠模型名稱推定額外權限或省略交易安全。

## 已落地的調整

| 範圍 | 原本的問題 | 調整後的行為 |
|---|---|---|
| 自主性 | 個人 AGENTS 主要是 CodeGraph 操作手冊；工具不存在或 index 未初始化就要求詢問 | 工具不可用時用原生搜尋／AST繼續，不把選用工具設定變成任務前置門檻 |
| 澄清 | Karpathy 的 `If uncertain, ask`、`If something is unclear, stop` 沒有區分實作判斷與必要資訊 | 可逆選擇由模型決定；只問會實質影響正確性、範圍、成本、外部效果或授權的缺口，繼續不依賴答案的部分 |
| 批准 | 「逐場授權」「每筆重新授權」與文件中的「本輪未授權」混用 | 人工批准按 action／target／limits／run 保存；每筆 order authorization 是程式內部檢查。歷史紀錄不撤銷後續批准，也不批准新場次 |
| 完成 | Ponytail 的 `Ship the lazy version`／`Need full X? Say so` 可能把完整需求縮成先交部分 | 最簡單實作仍須滿足全部已接受需求；完成必要實作、驗證與文件後才交付，不問是否要完成已要求的部分 |
| 重疊 | Karpathy、Ponytail、兩層 AGENTS 同時管理簡化、澄清、測試、輸出；Ponytail hook 在 session／prompt／subagent 重複注入 | 通用協作規則集中個人 AGENTS；Karpathy 保留程式理解、精準修改與最小完整實作；專案 AGENTS 保留交易與專案要求 |
| 測試 | focused／V2／shared／full 可被當成四個各自重跑的關卡，milestone 未定義 | 同一版本的完整測試可涵蓋子測試；依變更風險驗證。指令／文件修改不重跑交易測試；只有新變更、失敗或疑點才重驗 |
| 長測 | 計畫前段仍要求30分鐘T3，尾段§19.7已改5分鐘本機dry | 文件頂端及R3／R5直接標明最新覆寫，長測未完成仍如實保留，但不阻擋本機工作及準備最小canary |
| 簡報工作流 | guizang Skill 要求逐題確認、重問配圖／風格、忽略使用者配色，並指定作者Mac絕對路徑 | 依已提供需求和可用模板直接製作，只問必要缺口；風格預設不覆蓋使用者選擇，工具或參考缺失不阻塞獨立工作 |

Ponytail 已在個人 `config.toml` 設為 `enabled = false`，**保留安裝內容，沒有卸載**。這避免只修改 plugin cache、下次更新又恢復衝突。其有用的最小實作原則已合併進個人 Karpathy；Ponytail 專用命令與 hook 會隨插件停用。模型仍是 `gpt-6-astra`，其他解析後的設定完全相同，沒有改 sandbox、工具批准或交易權限。

## 其他 Skills 的衝突與處理

以下為已讀取入口的規則風險，不能僅凭文字認定所有過去停工都由它們造成。受管理插件原文未改；在個人 AGENTS 明定任務範圍、既有授權、技能選用與驗證原則，避免堆疊相反工作流。

| 來源 | 明確衝突 | 採用原則 |
|---|---|---|
| Product Design index／ideate | `exactly three visual options and wait`、即使要求自行假設也等選圖；Sites-building反而禁止額外選圖gate | 依實際交付物選主工作流；使用者已委託設計決策時，不新增固定選項門檻 |
| Product Design image/url-to-code、Sites-building | 前者驗證不能跑就不得handoff，後者未明示又禁止browser QA | 完成可審查成果並說明未驗證項；不能冒稱完全驗證，也不能越過真正必要的發布gate |
| Sites-hosting | 重複要求shared/public批准；流程又直接要求commit／push | 先核對授權是否已涵蓋目標、受眾與動作；技能流程本身不創造Git或發布授權 |
| Documents、PDF | `repeat until flawless`／`zero visual or formatting defects` 缺少有界完成標準 | 以用途相關的資料正確性、可讀性及實質缺陷判定；必要render／表單驗證保留，不無限修飾或重跑 |
| Documents | 舊工具不存在便要求安裝／重裝Google Drive | 查目前可用能力並完成獨立本機成果；安裝依實際工具條件，不因過時名稱阻塞 |
| Visualize | `Never send commentary` 與平台進度更新要求相反 | 依較高層溝通要求；保留視覺工具的sandbox／CDN／API限制 |
| Presentations、Product Design audit | 固定加入裝飾、結束後固定問Figma | 僅提供服務於交付物的資產；不把額外推廣或可選升級問題當完成條件 |
| OpenAI Docs | docs-first字面規則與目前環境local-first要求相反 | 依本輪較高層工具指令，先查本機；本次使用其broad-customization手冊路徑核對設定，不改系統Skill |
| Ponytail-review／audit | complexity-only報告以`Ship`結尾；generic code audit觸發只找刪碼的窄審查 | 插件停用後不再自動觸發；一般審查按使用者範圍，不能用減行數取代correctness或完成判定 |

八個GSAP入口未發現新的強制批准／提前停止規則，保留技術內容。Spreadsheet的公式、資料驗證，以及PDF表單重開查核屬實際正確性要求，保留。平台或工具若把某項要求以較高層指令提供，AGENTS不能覆寫；本次未更改任何平台政策或批准控制。

## 修改位置與可回復性

- [個人 AGENTS](C:/Users/爸爸/.codex/AGENTS.md)：跨專案的自主性、澄清、批准與完成協作規則。
- [專案 AGENTS](../AGENTS.md)：V2／Grid邊界、逐場live授權、去重測試、5分鐘本機流程。
- [Karpathy Skill](C:/Users/爸爸/.codex/skills/karpathy-guidelines/SKILL.md)：集中最小完整實作原則。
- [Guizang Skill](C:/Users/爸爸/.codex/skills/guizang-ppt-skill/SKILL.md)：修正具體重問、路徑與風格流程矛盾。
- [個人 config](C:/Users/爸爸/.codex/config.toml)：只停用Ponytail插件；未變更模型或執行權限。
- [V2計畫](CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)、[架構](mm_v2/ARCHITECTURE.md)：澄清舊進度／授權文字與最新短測順序。

修改前版本備份在 [instructions-20260906-025401](C:/Users/爸爸/.codex/backups/instructions-20260906-025401/manifest.json)，包含對應原始路徑與SHA256；備份不進Git。全域設定恢復應只還原欲恢復的欄位，避免覆蓋之後的新設定。

## 驗证範圍與生效

兩個修改Skill的frontmatter驗證通過；TOML解析確認僅Ponytail enabled值改變，備份SHA256及文件連結／diff空白檢查通過。獨立代理另審查七種情境：缺CodeGraph、既有full suite、未授權canary、已授權run退出、已用一次commit授權、純Skill修改及缺render。前六種行為一致；最後補明文允許UNVERIFIED DRAFT並保留正式驗證未完成狀態，也澄清Grid-specific程式與shared Lighter改動的區別。這是指令相容性驗證，不能保證模型永遠不誤判。

本輪沒有修改MM／Grid執行程式；MM及shared檔案hash仍與已完成302秒dry一致，故不重跑交易測試。未啟動canary、未操作VPS、未commit/push。

本機新版指令已寫入。已注入目前對話的舊hook／系統指令不會因磁碟編輯自動消失；重新啟動Codex後核對新設定，以載入新版。官方說明：[AGENTS載入](https://learn.chatgpt.com/docs/agent-configuration/agents-md.md)、[設定參考](https://learn.chatgpt.com/docs/config-file/config-reference.md)。本次從當日更新的官方Codex手冊核對；沒有假定修改AGENTS能覆蓋平台層指令。
