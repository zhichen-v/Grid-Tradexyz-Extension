# Market Maker V2 — Volume-first objective

**最新本地修復與驗收（2026-09-13）：** 已完成193558揭露的行情失效分支診斷、健康行情暫時不同步時的單次fresh重新對齊，以及原始完整流程／舊處理反例。重用原10s及一次read重試，與帳戶race共享額度；不重試失效book或斷線，不改風控及退出期限，無可信IOC行情時如實殘量失敗。全專案949項（231.439s）中657項V2全部PASS，其餘8F＋4E與既有Grid／Lighter基線逐項相同；原Windows Ctrl+C與到期自然收尾亦通過。精確來源、raw場景及驗證見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)。本地修復已完成，最新實盤及費用結論仍為下方193558，沒有用合成測試升級經濟驗收。

**最新實盤分析（2026-09-13，193558）：** Planned3600s、wall2674.2750979s後因`authorizing_quotes / LighterReadError`提早code1；錯誤落在aligned-book缺失或stream transport不健康的共同檢查，現有證據尚不能區分。最後自動取消2單、0次IOC，cleanup及final皆authenticated0/0、帳務差0；本輪三次bounded exit均flat。45 maker＋1 taker，maker1059.125780、gross+0.006600、fees0.13246621460、交易net−0.12586621460；毛利僅覆蓋4.9824%費用，正式economics仍unavailable。本輪完成紀錄分析，沒有修改runtime；來源、成本分組與待補測試見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)。下方000352等實盤狀態保留為歷史。

**前批完整流程驗收（2026-09-13，歷史）：** 已補原PS1／CLI→session／SDK／raw資料完整流程與Windows真Ctrl+C測試，修正啟動器退出碼誤報及測試冷匯入污染。修正後646項V2全PASS；同源Windows到期／Ctrl+C均自然退出、fresh account與獨立合成venue0/0。這是原始Python流程的合成驗收，native ABI／實體傳輸／真撮合／完整60分鐘實盤及economics仍未驗收。執行方法見[主計畫§19.9](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)，精確來源與時間見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

**前場000352（歷史）：** 該場planned3600s、wall578.488854s後code1，原final authenticated flat／2張委託，取消缺證仍阻斷收尾。使用者其後親自確認0/0，本輪沒有執行私人API唯讀查核；不能回填原run為成功。新增診斷記錄generic signer/provider error、4次history無exact terminal，2次recovery後仍pending。真SDK本地重現證明送出前／簽名失敗與transport／送出後解析失敗都可能產生同類診斷，歷史精確原因仍未知；整場sendTx31不能單獨證明最後取消是否送出。Maker77.344280、已記錄交易net−0.02983430860，formal economics unavailable。本地已補真SDK階段證據、精確no-send／nonce契約及fresh有界清理，並移除MM掛單DNS／文字429錯誤恢復；本批本地驗證完成，沒有放寬風險或將不確定mutation當成未送出。詳見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

本批632項V2現行契約均有通過證據：全專案920項回歸後，修正一項舊API查核次數expectation並完成相關34項補測；runtime未再改動。其餘8 failures＋4 errors與既有基線逐一相同。完整輸出及證據界線見EXPERIMENT_LOG，未宣稱全專案全綠或實盤已驗收。

**前場215907（歷史）：** wall128.579s、0 fills後code1，首次60s委託到期時，正常撤單的完整終態在Adapter交接中被占位回覆取代，遭前批strict manager拒絕。先前有界取證路徑成功收尾，該場cleanup／final皆authenticated0/0；仍未完成60分鐘，無經濟通過證據。已修MM opt-in直接保留完整terminal至consumer確認，另阻止client ID碰撞誤清其他pending紀錄；原strict檢查、Grid default及API／風險限額保留。

前批全專案889 tests／98.161s，601項V2全數通過；其餘8 failures＋4 errors與既有基線逐一相同，無新增失敗。

**前場205914（歷史）：** wall1751.682s後code1，原run最後short0.00040／2單，取消不確定使收尾鎖停。使用者另行授權的21:43:28唯讀查核觀測0/0；後續歸零及成本未歸因，不能回填原run成功。本場雙邊90.74%、maker680.019960、已記錄交易net−0.07472294620。前批595項V2 PASS漏掉真Adapter正常撤單交接，215907已揭露並修正，不能再將該批測試當成完整契約證據。

下面為前批本地修復狀態與產品目標；231145已不是最新實盤。

前批595項V2 tests PASS（86.813s）仍是其當時的離線結果；205914最初cancel未確認的provider原因仍缺歷史證據。

> 狀態：2026-09-12，依fee-cover分析完成減倉容量一致性修正：原兩側共用搜尋皆低於minimum時，以原預留公式確認是否仍容許正常單側減倉，避免因此提前轉passive-touch；真正風險退出保持有界。新增order／governor／公開行情時間證據、啟動時有效設定與版本，以及既有分析器的成本分類、真實flat-to-flat分組與markout覆蓋。上一批funding／monitor／缺側補單修復保留。最新實盤仍為231145：planned3600s、wall2901.283s後code1，歷史清理0/0但最後cash差+0.000361530936 USDG；maker2324.147061、交易net−0.32727171132（funding未入帳）、雙邊54.98%。沒有新live或私人帳戶連線，完整60分鐘、最終帳務與fee-cover仍待驗證。完整本地測試與歷史證據見[EXPERIMENT_LOG](EXPERIMENT_LOG.md)。

歷史231145重算的19組maker-only交易net為−0.13202000124 USDG；7組正gross不足付費。正常fee floor已存在，這些結果支持先定位成交／退出成本；不直接放寬風險或假定加大edge即可獲利。舊90筆fill的reference缺失仍保持不可用。

本批全專案870 tests／91.838s，包含582項V2全數通過；其餘8 failures＋4 errors與既有Grid／Lighter cancellation基線完全相同，無新增失敗。固定一小時离線回歸仍有42 maker／8 taker、0 API退出及final0/0／精確cash；這些不代替修後實盤與經濟驗證。

## 目標與判定

2026-09-05 依使用者覆寫完成 V1 清除：必要 execution safety 已移入 V2，無舊 package 依賴；V2 345 tests PASS，全專案 619 tests 為同組既有 8 failures + 4 errors，無新增失敗。此離線驗證不改變上方 dry/live gate 狀態。

在 session all-in net cost、drawdown 與 hard inventory 限額內，提高固定完整時間窗的 `maker_turnover_per_wall_hour`。正常狀態持續雙邊 `POST_ONLY` 報價，允許單筆虧損；持倉逾時、虧損、session deadline 或 stop 必須進入有界退出流程。允許為整場風險控制付出 taker flatten 成本，不再等待不虧損的自然 flat。

- Safety：unknown orders、unresolved mutations、reconciliation failures、self-trades、position-cap breaches 都必須為零；結束時 authenticated position / open orders 為 `0 / 0`。
- Liveness：stop/deadline 後須在有界期限內 authenticated flat；撤單成功不等於平倉成功。
- Volume：以 maker turnover / 固定完整時間窗為主，包含startup、撤換單、cooldown、pause與收尾；早停不縮短預定窗口，收尾超時延長分母。Quote-hour效率、雙邊working uptime、capital turnover作診斷；R4 analyzer提供這些比較，缺allocated capital或working-side證據時保持unavailable。
- Economics：納入全部 maker/taker fills、fees、final flatten，另列 funding/cashflow；只在可信的 final-flat 邊界判定 all-in net cost、fee cover、drawdown 與 flatten loss share。未平倉或資料不全不能宣稱通過。

使用者於2026-09-05提供測試資金約299 USDG，成交量目標尚未設定；不是本輪authenticated餘額或損失授權。先建立volume/cost frontier與實際可執行lot／inventory band表，再約定量級。正acquisition cost必須明示尚未fee-neutral，不可當成原目標通過。

## Fee floor 與報價

令 `f` 為 authenticated maker fee（每側 bps）、`e` 為 target net edge（完整 roundtrip bps）、`v` 為每側短期波動 buffer：

```text
half_spread_bps = f + e / 2 + v
full_spread_bps = 2f + e + 2v
```

每側相對reservation的half-spread至少為maker fee，屬首版報價baseline，**不保證每筆或整場cover fee**；不同時間的reference、inventory drift及退出成本仍需真實成交驗證。歷史 2026-09-03 preflight 的 maker / taker 為 `1.2 / 3.5 bps`，所以 maker roundtrip fee floor 為 `2.4 bps`；這不是目前費率或 live 建議。每輪 live 仍須重新取得 authenticated fee。Phase 1 使用 `v = 0` 的 baseline 比較 spread / quote distance，並明示 **not a queue-fill backtest**；不由 BBO spread 單獨推論成交率或經濟 GO。

## 非目標

- 不延伸 V1 ping-pong、fee-aware breakeven exit、natural-flat gate、toxicity controller、campaign authority 或固定 per-episode reserve。
- 不做 Grid 改造、多層報價、MACD/NATR/ML、自成交或洗量。
- 不在短 canary 找到經濟可行點前建造24h infrastructure；三組target-edge只作初篩，回報已測區域無可行點或證據不足，不以三場失敗宣稱整個市場不可能fee-neutral。聚合先加總再相除，所有預定窗口／失敗均保留，另以未參與選參的窗口確認。

## 授權與邊界

Live、平倉及 margin/leverage 操作須逐場明確授權；commit/push 另依操作者當次指示。舊 V1 campaign 的剩餘風險預算不自動轉成 V2 授權。

V2 live 啟動時必須在任何連線／mutation 前要求 `--authorize-bounded-flatten`，並取得當場明確授權；非 flat start、unknown order 或不可信 account/market state 必須拒絕／暫停。授權涵蓋預先約定的 reducing LIMIT IOC、slippage、attempt/deadline 與 stop loss 範圍，不能由 YAML 關閉。

即使授權有界 IOC，也不能保證無流動性／失聯時成交。這類情況必須回報 liveness failure 與 residual inventory，不能提高風險上限或把 cancel-only 說成完成。

## 複雜度預算與進度

R1退出、R2雙邊報價／drawdown reserve／final freshness、R3 own/book對齊／arrival race／保守hold age及健康transport的API退出預留均已有離線契約驗收。日常短dry可明確放寬source至−100..10000ms；live拒絕該選項並保留strict檢查。頻繁改價仍可提前花到quota保留邊界，尚未證明持續大成交量。R4 analyzer493 LOC；真實BTC minimum base為0.00020，因此0.00026半單仍無法保持soft邊界雙邊報價，較大數量表僅是算術假設。完整限制見rebuild plan §19.7及EXPERIMENT_LOG，不自動放大size或授權canary。

V2 起始策略 config 為計畫的 **18 個 leaf fields**；安全不變量由 profile/code 擁有，不另開開關。Orchestrator ≤500 LOC、QuotePolicy ≤350、InventoryGovernor ≤400、session analyzer ≤500、Phase 1 feasibility ≤500；函式盡量 ≤60 LOC。只保留所需 port、標準庫與公開契約測試。

按 Phase 0 → 1 → skeleton → ledger → quote → governor → runner → replay/dry → 授權 canary 逐步驗收。架構見 [ARCHITECTURE](ARCHITECTURE.md)，唯一 phase/run 記錄見 [EXPERIMENT_LOG](EXPERIMENT_LOG.md)。依 2026-09-05 使用者明確指示，Market Maker 工作樹只保留 V2；原本等待 4h／24h 才刪除 V1 的限制已被取代。V1 歷史仍可由 Git tag `mm-v1-guard-driven-20260903` 追溯；必要的訂單安全語義由 V2 自有 execution modules 與測試承接，不保留舊策略或相容入口。這不是 V2 live／economic GO，也不改變逐場授權要求。
