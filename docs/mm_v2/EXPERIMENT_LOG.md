# Market Maker V2 — Experiment log

## 2026-09-13 場次193558後：行情故障診斷、單次重新對齊與完整流程反例

使用者授權本地實作、驗證及完成後一次commit/push。本批保留先前未提交MM V2修復與測試，沒有改動交易數量、費率、風險限額、source freshness、Grid策略或live設定。以下為可重現的處理缺口修復，不能事後補出193558缺失的原始觸發原因。

**原因證據不再合併遺失。** Shared ReadStream新增唯讀`first_stream_failure`，只保存固定stage／reason，首次失效在close後仍保留；正常close及真正CancelledError不誤記故障，後續unavailable不覆蓋初因。MM account保存book等待逾時、book不可用、超opening/closing watermark、缺watermark、generation變動，以及numeric nonce／age；market及session的`failure_diagnostic`用固定allowlist保存這些資料和stream／source分類，涵蓋錯誤先發生在`syncing_orders`的路徑，不只market.refresh。無raw payload、例外文字、URL、token或身份資料。Account逾時／外層取消仍清除可用handoff及cash cache，但保留當次診斷到記錄完成；下一個真正cycle再清，避免又留下空原因。

**最小恢復契約。** 正常session要求aligned book；當完整authenticated account audit已成功，僅book等待逾時或book不在原order watermark範圍，且同一mutation generation、transport健康、原`book_snapshot()`的source／book檢查仍通過時，使用原account.snapshot的共同10s期限及至多兩次attempt重讀。第二次重新取得cash／orders／trades，逐項通過原retry／audit／條件API admission；帳戶race已用掉重試就沒有額外第三讀，已入帳fill／funding照ID去重。第二次仍不對齊則整場失敗並進原有界收尾。Invalid book、source／clock故障、transport斷線、generation變動或額度不足不以此路徑恢復；没有重連、重送mutation或放寬nonce界線。退出帳戶證明不要求book，仍先撤known orders；沒有可信行情時禁止IOC並如實回報殘量。Budget新增`market_reads.retries/recoveries`，只計實際獲准的市場重讀，保留完整場次證據。

**原始完整流程驗證。** 在既有wire fixture的兩張POST_ONLY都已確認後，從raw WS注入故障，不替換session／account／market／SDK方法。三個flat故障為合法但持續落後opening的行情（真正asyncio3s等待逾時，fresh source仍有效）、不連續book nonce、真reader EOF。另在失效同期由獨立venue成交一側；以及第一次book超closing、下一次fresh bracket才一致的可恢復案例。相同場景還原舊`require_aligned_book=False`，確實再次過早失敗，不能只靠增加通過數字宣稱修復。

| 完整raw場景 | 具體結果 |
|---|---|
| 持續book等待逾時 | 一次市場重讀、零恢復，之後停止新單；撤原兩單，authenticated及venue0/0，code1 |
| 不連續book nonce | 不重試失效行情；原兩單精確取消，兩端0/0、code1 |
| Transport EOF | 在syncing_orders識別transport原因；原exit-only REST查核清理至兩端0/0，code1 |
| Book失效＋SELL成交0.00040 | 撤除另一張known單，沒有IOC；兩端short0.00040／orders0、code1，如實回報未平倉 |
| 短暫超closing watermark | 原一次fresh read取得新opening／closing13/13，中間有fresh REST cash，retries1／recoveries1；繼續兩輪正常到期並0/0、code0 |
| 還原舊處理的反例 | 同一暫時不同步場景code1、無後續normal create，最終0/0；同一正常驗收會拒絕它 |

每場原HTTP meter與fixture的實際端點attempts一致；故障後原未成交ID各cancel一次，不借用manager或ledger生成venue答案。Focused新4個methods（含6場景）全部PASS／29.411s，當時來源指紋`07b6d51720482b4b5e63b18e6faf1706430384acaabaa7e0516b881fb3545b59`；此後修正診斷中的市場重試計數語意並新增去重回歸，交付以最終full suite的同源證據為準。Lower-level另驗證共用期限、generation、quota拒絕、不可累加account race重試、退出無book、外層取消後診斷保留、raw敏感字串不外洩與已入帳fill重讀去重。原Windows真60s quarantine／Ctrl+C與過往取消安全案例保留。

提交前獨立檢查確認33個tracked變更及5個新test helpers均為程式／文件；Grid測試的既有差異只更新MM opt-in「DNS不能證明未送出」契約，default對照不變。無新增私人設定／環境檔／日誌或秘密字串。先前字面`%SystemDrive%/`快取仍排除於Git；不重試已被拒絕的刪除。沒有新live／私人API查核或VPS操作，193558經濟結論不變。

**最終同源驗證：全專案949項／231.439s，657項V2全部PASS。** 完整輸出`logs/mm_v2_market_fault_full_20260913.log`；其餘8 failures＋4 errors與`logs/mm_v2_phase7_20260904/v2_only_cleanup_full_tests.log`的12個既有Grid／Lighter案例逐項相同，新增／缺少案例均0，不宣稱全專案全綠。此完整run已涵蓋shared ReadStream、新raw場景及Windows啟動器，無需另跑重複V2 suite。最後source指紋`7493caa376672aaaf0c741b6c70eacbc17387ea75a5817bc99a68a22d45b58ee`；6個raw案例manifest及兩場Windows均逐項確認與此版本相同。Raw證據為`logs/mm_v2_wire_20260913_125056_105251_book_alignment_no_retry`、`125058_144496_book_invalid_nonce_fill`、`125122_412322_book_alignment_recovers`、`125141_721441_book_wait_timeout`、`125143_668055_book_invalid_nonce`及`125146_492832_book_transport_close`（後五者同`logs/mm_v2_wire_20260913_`前綴）。Windows `logs/mm_v2_launcher_20260913_125014_503284_ctrl_c`與`125016_170289_deadline`（同前綴）皆code0、watchdog=false、整樹自然退出；Ctrl+C至整樹退出0.578s。獨立安全審查與內容／格式檢查完成，runtime及測試沒有在此驗證後再改動。Native真實撮合、完整60分鐘live及費用覆蓋仍待實盤證據；此次Git提交包含本批及本對話先前累積的MM V2修復與完整流程測試。

## 2026-09-13 場次193558：行情檢查提早失敗，三次收尾成功，費用未覆蓋

使用者提交最新自行執行的60分鐘live輸出。本輪只分析本機journal及budget／window，更新既有證據；沒有修改runtime／交易參數、重跑產品suite、連接帳戶或commit/push。Journal `logs/mm_v2_economics_20260913_193558_509.jsonl`共11216 events，SHA256 `8d03046522f5fe08a3ee6ef60dc58a2130f1b72b5803125d9dbea17d7ce96c08`。現有`build_report`結果保存於同stem `.jsonl.analysis.json`，包含原typed public-book觀測的markout診斷。Startup為`b4605b9`、dirty=true與公開有效參數，沒有完整source指紋，不能僅凭HEAD證明與646項測試完全同源。

**完成度與終態。** Planned3600s、whole-process wall2674.2750979s（約44分34秒）、ledger2611.3621414s後code1。最後line11214 bounded exit取消兩張known orders、0次IOC，取得authenticated0/0；line11215再次取得authenticated position0／orders0，line11216 ledger final對帳差0。這是原場終點證據，沒有重新查核目前帳戶。相較000352原場0/2，本輪自動收尾有成功證據，仍未完成一小時。SessionReport的`complete=true / failed=false`只描述ledger結算；CLI保持`completed=false / failed=true / economics_evaluated=false`，analyzer因`runtime_failure_diagnostic`保留正式經濟欄位unavailable，不以ledger complete覆蓋runtime失敗。

**唯一最終故障。** Console約2669s，line11213為`authorizing_quotes / LighterReadError`，source鏈`orchestrator:753 → 498 → 342 → 397 → lighter_runtime:809 → 770`。Top-cycle order sync及account audit已返回；market refresh在「沒有與委託同步的行情，或stream transport不健康」的共同分支拒絕。Execution仍healthy、兩侧live、uncertain=false／unknown=false；此次沒有取消缺證的failure。`_stream_read`可吞掉book等待RuntimeError／TimeoutError，或將不滿足opening≤book≤closing的行情設為None；原診斷未保存這些分支或最初stream關閉原因。不能確證網路斷線、協定錯誤或瞬時watermark變動，亦不能認定加重試就能安全恢复。

| 本場交易診斷 | 結果（USDG，除另註） |
|---|---:|
| Maker fills／turnover | 45／1059.125780 |
| Taker fills／turnover | 1／15.346060 |
| Realized gross | +0.006600 |
| Maker／taker fees | 0.12709509360／0.00537112100 |
| 已記錄交易net | −0.12586621460 |
| 毛利／總費用 | 4.9824% |
| Max drawdown | 0.128044600200 |
| Fill累計持倉峰值 | long+0.00060／short−0.00080 BTC |

Funding及external transfers均記錄0，final cash reconciliation差0。公開有效配置每單0.00040、soft0.00040／hard0.00080 BTC、session loss0.50、target edge0.20bps；峰值觸及hard但沒有觀測到超越。正式比較窗口保持planned3600s，maker turnover/hour為1059.125780，不以提早停止的44分鐘縮短分母。有單／雙邊時間2523.7400517／2347.8791986s，占ledger96.6446%／89.9101%。

**三次退出的原因不同。** 約665s因`inventory_hold`退出；取消期間maker BUY0.00040成交，使short0.00020轉成long0.00020，隨後單次reduce-only SELL IOC0.00020，約670s成功0/0並冷卻重入。約2449s因`monitor / local_api_budget`預留容量不足退出（預估24106高於24000），取消1單、0次IOC，約2451s成功0/0。約2669s才是最終行情檢查失敗，約2672s取消2單並成功0/0。第二次是本地保守容量保護，沒有證據稱為交易所429。

**成本主要仍在正常成交。** 41筆normal maker診斷net−0.10408829040；4筆passive reducing maker為−0.01616680320；單次IOC為−0.00561112100，只占整場淨損約4.46%。13組完整flat-to-flat中12組maker-only，只有1組net正，另6組gross正但不足付費；maker-only合計net−0.09271489600。單純減少IOC不能解決本輪成本。1s／5s成交後public-mid變化各覆蓋31/45及36/45，費前turnover加權−0.079550／+0.118439bps；缺樣本不插值，public BBO含自身委託，不能當成完整策略淨優勢。

**API、行情與測試缺口。** 可選改價等待63次、budget退出1次、account-read race退出0次；REST／WS／TX峰9900／105／9。Strict已接受20197包、最大接受age367.803ms、已接受超界0；這些統計不含被拒絕或未收到的包，不能排除末端stream故障。前批完整wire情境涵蓋取消、終態延後、回應遺失與Windows停止；現有較低層測試亦涵蓋nonce／stale／closed及超closing watermark，但尚未將這些故障接入完整session及分支診斷。

下一批最小範圍是固定allowlist區分book_wait_timeout／book_invalid／outside_watermarks／transport_unhealthy及時間／nonce證據，不記raw payload；在既有wire fixture驗收有單時行情失效即停止新風險、known-order精確清理、fresh final真實回報且清理成功不抹除原失敗。同期成交的fresh residual退出、持續失效且無可信IOC行情時如實失敗，仍保留原限額／期限。此處是本輪定位出的待補範圍，尚未實作，不放寬freshness或加入盲目重連。

## 2026-09-13 完整流程與啟動器測試：合成wire、真Ctrl+C及退出碼修正

使用者在§19.9規劃後授權實作。新增`tests/mm_v2_wire_fixture.py`、`tests/mm_v2_wire_evidence.py`、`tests/mm_v2_windows_process.py`、`tests/test_mm_v2_wire_session.py`與`tests/test_mm_v2_launcher.py`，沿用repository `.venv`／unittest。既有未提交runtime／tests保留；本輪production只修`run_live_test.ps1`退出碼處理，未改交易參數、MM風控、Grid或共用交易adapter。

**實測抓出的啟動器缺陷。** 真Windows hidden console的CTRL_C_EVENT可傳到Python，Python也完成延後finally；但PowerShell中断流程會略過原`$runCode = $LASTEXITCODE`，原window因此留下預設1，PowerShell自身卻回0。修正僅在啟動前初始化`$LASTEXITCODE=1`，finally再取得Python實際退出碼並明確`exit $runCode`。原`& Python`啟動、bounded cleanup及時間／金額界線不變。獨立child0／7、真Ctrl+C後延後清理與原session兩場均驗證，不只互比兩份可能同錯的摘要。

**完整原始程式鏈。** 原CLI讀取合成設定並建立真adapter／session／manager／port／account／ledger；原SDK nonce decorator、sign/send、HTTP form／raw response反序列化及ReadStream原始JSON處理都在同一場執行。只有native signer、HTTP pool及WS傳輸替換，快測另使用原constructor的clock／sleep seam。以執行code object和其指紋證明必經原函式；fixture獨立保存接受狀態、請求、history發布、成交、持倉與cash，不以production ledger或manager slots生成答案。

| 合成完整鏈情境 | 具體驗收 |
|---|---|
| 正常150s虛擬窗口 | 至少六張POST_ONLY，跨兩輪正常60s到期；每個ID只cancel一次，venue／fresh account0/0及exact cash；原125s可進入期限退出，改測150s，沒有改runtime截止邏輯 |
| 終態晚到＋另一側成交 | 四次history及第一次cleanup讀都缺證；第二次cleanup讀取得exact terminal後，僅原兩張maker＋一次fresh residual reduce-only IOC，最後0/0；原場失敗碼1仍保留 |
| 接受取消後回應遺失 | raw transport在venue接受後丟ConnectionResetError；原exact history恢復，每個cancel只送一次、nonce不回用，正常完成 |
| 終態始終不可得 | 初始四讀＋cleanup兩讀仍缺證，無重送／新maker／IOC；原另一側委託仍在venue，client如實final0/1、exit blocked、code1，不把安全停止算清理成功 |
| 歷史缺陷與判定器反例 | 在測試作用域還原215907 placeholder及完整terminal handoff/cache遺失，正常契約必須變紅；偽造flat的snapshot副本必被獨立venue判定拒絕，後者僅為判定器敏感度，不宣稱wire隱瞞場景 |

晚到終態案例的固定合成成交為maker SELL0.00040＠77009.3（fee0.0036964464），IOC BUY0.00040＠77000.1（fee0.0107800140）；手算gross0.00368、net−0.0107964604、final cash999.9892035396，與原ledger逐項相符。每場全部HTTP端點的實際fixture attempts與原兩個SDK API client meter逐項一致；這仍不是交易所實際權重／redirect或retry的網路量測。

**Windows真時間及隔離。** 使用含中文／空格的暫存工作區、原PS1／CLI及實際venv，保留真60s quarantine、Proactor／asyncio喚醒；原PS1从另一cwd啟動仍正確。程序先暫停建立、納入獨立job後才執行；stdio可繼承窗口以lock隔離。成功要在關閉job之前確認整棵樹自然清空；watchdog只在預期超時反例強制回收，不能算正常退出。缺Python／缺或無效配置在真正交易子程序前拒絕，child0／7及Ctrl+C後延後finally亦有獨立驗證。

初次完整Windows focused兩場共67.206s：到期3s場次PS wall64.9237873s／active3.2063742s；Ctrl+C場次配置30s、PS wall63.2283636s／active1.4652183s，signal到整樹退出0.547s。均code0、watchdog=false、remaining processes0、fresh authenticated0/0、venue0/0／cash1000，Ctrl+C後無新正常maker，原bounded exit≤30s。證據為`logs/mm_v2_launcher_20260913_072703_363979_deadline`與`logs/mm_v2_launcher_20260913_072701_669744_ctrl_c`，來源指紋`2ee275843f3e787be96da1bd503ab23ef22eb5623ac12fdfcc1eeee7996ffa6c`。這些是最後冷啟動fixture修正前的證據；最新版同源結果另記在下方最終驗證，不混用版本。

**測試自身的防誤綠。** 獨立審查補齊fixture的exact URL／method／auth／account／market／nonce、typed native modes／flags、最低名義金額、POST_ONLY不跨價及IOC限價；非法輸入須在接受及nonce變更前拒絕。一般socket／DNS／直接native loader都有拒絕反例；唯一允許的連線是原stdlib socketpair在同thread同步建立、綁定到自己的literal loopback埠，保留原Windows喚醒。沒有把假的self-pipe當Ctrl+C證據，也未更改系統網路設定。沒有可證實的OS封網，native ABI／實體HTTP／WS傳輸／真撮合仍未涵蓋。

最後發現冷匯入順序缺口：若ReadStream首次在websockets.connect被替換後匯入，原default就會綁到fixture，退出patch仍殘留。新的獨立子程序先重現`cold_import_default_restored=False`；將匯入移到patch之前後，冷啟動回歸通過。原常態案例預先匯入、Windows bootstrap存續於整個子程序，因此先前正常結果未被該漏影響；仍以修正後全suite重新驗收。初輪645tests／188.810s全PASS保存在`logs/mm_v2_wire_launcher_final_tests_20260913.log`，這個數量未包含新冷啟動回歸，不能冒充最後版本。

**最終同源驗證：646項V2 tests全部PASS，188.640s。** 修正後完整輸出為`logs/mm_v2_wire_launcher_verified_20260913.log`，包含新增9項wire／隔離／反例與5項Windows測試及其子案例。最後兩場Windows證據為`logs/mm_v2_launcher_20260913_073457_918664_deadline`（PS wall64.8650389s、active3.1982325s）與`logs/mm_v2_launcher_20260913_073456_154273_ctrl_c`（PS wall63.1079093s、active1.4327769s、signal至整樹退出0.485s）。兩場皆code0、自然清空job、watchdog=false、fresh authenticated0/0與獨立venue0/0／cash1000。最後來源指紋`460e29cbc5c0e63f0e5007dd4db44ec8d5360a4026086ceff42baaaea21b1af9`，冷啟動修正與完整鏈皆在此版本驗證。獨立審查與`git diff --check`通過。本次沒有新增shared production變更，不重跑無關Grid基線；不把這次V2全綠宣稱全專案既有基線已解決。

Journal、budget、window（Windows）、獨立venue及synthetic manifest保存在ignored `logs/mm_v2_wire_*`／`logs/mm_v2_launcher_*`。Manifest保存Python3.12.13／SDK1.1.2、native檔案指紋（不載入）、執行code objects、前後一致的來源內容與公開設定，明示`synthetic=true`／`live_economics_evidence=false`。所有帳戶、金額及成交為合成案例；沒有新私人帳戶連線／查核、live、commit/push或VPS操作，使用者先前自行確認0/0仍不是原000352場次自動成功的證據。原provider錯誤、真60分鐘穩定度與economics仍未驗收。

早期launcher精簡環境漏SystemDrive，產生repo內字面`%SystemDrive%/`下三個Windows cache檔；已補環境變數並把profile／temp／ProgramData移入隔離暫存workspace，原因已修正。兩次檢查範圍後的精確刪除均被自動審核以`blocked by policy`拒絕，未改shell或繞過；該暫存資料夾仍保留、未加入Git。

## 2026-09-13 測試有效性審查：規劃完整入口驗證，尚未執行

使用者指出反覆修復後，現有測試仍未有效驗證 `run_live_test`，本輪要求規劃。核對原PS1／CLI、session、SDK／read stream與測試替換邊界後，確認主要缺口是組合驗證：CLI測試替換整個session，多數session情境直接產生OrderData；真SDK與raw WS各有測試，但未在同一完整啟動與故障時序下執行。長窗口多以虛擬時鐘與成本代理執行，signal測試未實際通過Windows PowerShell子程序。這不推翻既有回歸結果，也不能由632項V2通過推論實盤穩定。

具體計畫已記入[主計畫§19.9](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)：以原CLI／session／adapter／SDK搭配合成raw HTTP／WS與獨立模擬交易所紀錄，覆蓋正常到期、回應遺失、延後terminal與同時成交、API成本、殘量及會計時間差；加入真PS1／Ctrl+C、真時鐘長測與還原歷史缺陷必須變紅的反例。Native／傳輸隔離不足時明示覆蓋缺口，不宣稱零連外或完整SDK驗收。後續網路校準規劃為5分鐘strict dry，再逐場授權5／15／60分鐘live；以事件覆蓋、原期限內收尾與獨立終態證據晉級，不以測試數量或沒有成交作替代。

本輪只完成來源審查、規劃與文件一致性檢查；新增測試尚未實作或執行，未重跑產品suite、啟動PS1、連接帳戶、改動交易參數、commit/push或進行VPS操作。000352初始提交錯誤仍未定因，使用者先前自行確認0/0不能改寫原場失敗。既有runtime修改與實際驗證記錄保留如下。

## 2026-09-13 場次 000352：取消提交階段證據仍有歧義，原場 flat／2單退出失敗

本場journal `logs/mm_v2_economics_20260913_000352_561.jsonl`，2050 events，SHA256 `525bdf651ec859494a41732425705e2eaabe2de5e039fb79da0df9c687c888d0`。Startup provenance為 `b4605b9`、dirty=true，含未提交修改，不能視為該commit的乾淨版本。Planned3600s、whole-process wall578.488854s、ledger515.4090054s後code1；分析保存在同stem `.jsonl.analysis.json`。原始line2049 account snapshot與line2050 report均為authenticated position0／open orders2，complete=false，並非完成收尾。

使用者其後親自確認position／orders已為0/0。本輪沒有另外執行私人API唯讀查核；這是使用者回報，不是authenticated postflight或原run的final proof。原場0/2、歸零過程與可能的後續成本仍按原證據保留，formal economics unavailable。

**故障鏈及新診斷。** 首個failure在line2039 `reconciling_quotes / ExecutionUnavailable`，其後line2041為下游ValueError；BUY uncertain_cancellation、SELL live、unknown=false。當次匹配取消action記錄 `cancel_receipt_pending=1`、`cancel_submission_signer_or_provider_error=1`、history attempts4／read errors0／exact matches0／captured terminal0及 `cancel_nonterminal_response=1`。Line2043 `exit_order_sync` 顯示recovery reads2、pending1、尚餘9203.5917ms、`recovery_terminal_pending=1`，其後exit_health blocked、IOC attempts0。這證明已讀歷史沒有取得相符終態；不能把泛化submission flag當成確定未送出。

最後兩張known orders在ledger447.1096791s／447.9623935s提交；504.8361183s的healthy快照仍對應同ID／價格。509.8909584s取消前檢查時BUY／SELL已約62.7813s／61.9286s，均超過60s quote age。首個failure位於513.4488394–513.9963310s的相鄰public observations之間；514.4513458s退出blocked，514.6972094s開始的final account read仍有原兩張known orders。取消前檢查至blocked exit為4.5603874s，並非完整wire取消耗時。相同BUY取消缺證→halt→退出受阻的模式再次出現；以往場次缺少這些新增flags，不能倒推其具體原因相同。

| 本場已記錄交易診斷 | 結果 |
|---|---:|
| Maker fills／turnover | 3／77.344280 USDG |
| IOC fills／turnover | 1／15.465700 USDG |
| Realized gross／maker fees／taker fees | −0.015140／0.00928131360／0.00541299500 USDG |
| 已記錄交易net | −0.02983430860 USDG |
| 有單／雙邊秒，占ledger時間 | 471.0499269／460.7033076，91.393%／89.386% |
| API／account race退出，可選延後 | 0／0，3 |

只有1組完整flat-to-flat，包含3筆normal maker及1筆IOC；沒有未閉合持倉不代表沒有委託風險。唯一正常退出由inventory_hold觸發，在216.4887982s以1次IOC取得authenticated0/0後重入。原planned3600s比較窗口下maker turnover/hour仍為77.344280，不縮短失敗窗口。1s／5s maker markout各覆蓋2/3，扣費前加權−0.678941／−0.377190bps；樣本過少，不能推論策略改善或以插值補齊。Strict來源2857包、最大age336.746ms、0超界；REST／WS／TX峰8600／110／8，無API拒絕退出。Budget累計sendTx31、nextNonce1、inactive-order reads47；31是整場聚合次數，沒有逐次交易類別／成功時序，不能由數量推定最後取消是否送出或已獲交易所接受。

**本地SDK重現與證據界線。** 真SDK的本地隔離重現顯示：送出前／簽名失敗，以及transport／送出後解析失敗，都可能產生相同generic diagnostics。故本場歷史最初取消的精確原因仍未知，不能確證nonce、signer拒絕、送出與否或某個price／ID欄位矛盾。History read errors0只排除已計數讀取的拋錯，不證明取消成功；多讀到absence也不是terminal proof。

**本批實作。** MM opt-in在單一signer instance、同一次真SDK呼叫中觀察sign／send邊界，保留原nonce decorator／key lock，不改SDK或全域transport；不同task直接透傳，完成／例外／取消均復原wrapper。只記固定stage及error_kind，不保留signed payload、token或raw錯誤；cancel／sign／send均須原SDK method，任意instance override不能提供未送出證明。真SDK本地sign error必須同時證明未進send且原nonce確實回退，才產生帶exact symbol／exchange ID的`OrderCancellationNotSentError`。不可觀測、可能已送出、回應解析失敗或nonce未恢復均保持不確定。

補上另一個已證實的控制流缺口：MM的SDK error tuple／rejected response原先直接返回False，現在與提交例外一起進入原4次exact terminal查核，不增加send或history上限。Manager移除依錯誤文字包含429便恢復LIVE的捷徑；真正可能已送出的mutation保留原strict terminal／ownership要求，不以文字或active／absence重送。

Manager只接受exact scoped no-send契約，保留原known slot、停止同輪剩餘mutation，不造terminal。正常port只對這類乾淨失敗永久停止正常報價，保留cleanup能力；任何mixed／unknown／fault變更仍halt。收尾前在原10s撤單期限及原30s退出內，另付5000REST／7WS／0TX的保守完整查核成本（包含stream掉線後的REST替代查核），保留整個既有退出預留；先fresh owned-order sync及authenticated account，再核fault／mutation generation、known ID及掛單集合。查核過期、不符、額度不足或cleanup再次失敗均不再送單；cleanup仍只用原兩撤單／三IOC預留，正常報價不恢復。撤除known orders的預先account audit不額外依賴行情；postcancel／IOC仍使用原market契約，同期fill以fresh residual處理。沒有調quote／size／loss／source或延長期限。

Telemetry仍只接相符known action的allowlist，新增no-send分類不假造pending；console同時顯示固定stage／error旗標，方便下一次直接定位，JSONL保留完整證據。新增真SDK、manager、port及完整session回歸涵蓋stage／nonce、post-send error tuple、unknown、同時成交、fresh cleanup、market中斷、deadline／budget／fault與wrapper復原；以實際執行SDK的測試補足此前只有替身receipt的缺口。

**同源潛在缺陷。** 獨立審查核對installed aiohttp：SDK POST可以自動follow redirect，之後才拋出配置API主機的DNS錯誤。因此DNS種類／主機相符也不證明原請求未送出。本批取消不採用DNS no-send推論，並移除既有MM create僅憑DNS便回退nonce的同源捷徑；保持ambiguous registry與禁止重送。此項會影響MM opt-in的共用Lighter送單契約，default/Grid原本即走ambiguous，行為不變。同步更新位於`test_lighter_grid_lifecycle.py`的單一MM opt-in共用轉接層測試；沒有修改Grid策略或配置。

**本批最終驗證（2026-09-13）：632項V2現行契約均有通過證據。** 同一runtime批次以repository `.venv`執行全專案920 tests／100.827s，結果保存在 `logs/mm_v2_000352_final_tests.log`。首次為9 failures＋4 errors：其中一项V2舊API成本測試仍預期SDK invalid-nonce error tuple不查history，與本次統一終態取證契約不符；只更新該expectation並增加registry保留斷言後，相關34 tests／0.470s全通過（`logs/mm_v2_000352_api_budget_tests.log`），沒有再修改runtime。真SDK預算測試確認error tuple亦只送1次、最多4次history、含nonce refresh仍不超原412REST／1TX界線。其餘8 failures＋4 errors與 `logs/mm_v2_phase7_20260904/v2_only_cleanup_full_tests.log` 的12個方法逐一相同，沒有新增未解決失敗；不把首次full輸出宣稱全綠。既有固定10／30／60min離線情境包含在此次full run。獨立審查及 `git diff --check` 完成。新增實際rolling budget拒絕回歸證明額外查核不借用原exit reserve；這不是全網路行為耗用上界的實測。未啟動新live、私人API查核、VPS或commit/push。修後完整60分鐘實盤與最初provider錯誤定因仍缺實際證據。

## 2026-09-12 場次 224839：取消缺證仍有殘單，補全有界查核與原因證據

本場journal `logs/mm_v2_economics_20260912_224839_588.jsonl`，7507 events，SHA256 `e29bf070d991f6b0328c394b967868c7801011e05a7cd3f0a492a80a87290d58`。Planned3600s、whole-process wall1866.7408862s（31分06.7秒）、ledger1803.8071360s後code1。最後 `reconciling_quotes` 的ExecutionUnavailable／ValueError有 `cancel_nonterminal_response=1`，BUY uncertain_cancellation／SELL live；exit sync期間SELL成交消失，第三次退出在exit_health blocked、IOC attempts0。原始line7506 account snapshot與line7507 session report都明示authenticated short0.00040／BUY1單，不能宣稱自動收尾成功。

使用者另外明確授權一次同帳戶Robinhood主網唯讀查核。23:40:28 +08得到positions=[]／open_orders=[]、身份驗證通過並已斷線，create/cancel皆0，耗時0.898s。順序讀取相隔約124ms，不是原run的atomic final proof；從殘留到歸零的過程與額外成本尚未歸因。結果保存在同stem `.readonly_postflight.json`，此一次授權已完成，沒有啟動新live或交易操作。

| 本場已記錄診斷 | 結果 |
|---|---:|
| Maker fills／turnover | 22／557.394960 USDG |
| IOC fills／turnover | 2／30.968640 USDG |
| Realized gross／maker fees／taker fees | 0.005080／0.06688739520／0.01083902400 USDG |
| 已記錄交易net | −0.07264641920 USDG |
| 有單／雙邊秒，占ledger時間 | 1708.9129447／1649.1297367，94.739%／91.425% |
| API背壓退出／可選延後 | 0／28 |

原planned3600s比較窗口下maker turnover/hour為557.394960，不以早停的31分鐘縮短正式分母。9組完整flat-to-flat含7組maker-only，只有1組maker-only net正，另4組正gross不足付費；1組SELL尚未閉合。gross／fees約6.54%僅作已記錄交易診斷，formal economics仍unavailable。1s／5s maker markout各覆蓋13/22（59.09%），扣費前turnover加權−0.585371／−0.415429bps；不回填缺樣本。兩次前段退出是stream_rest_bracket account race，分別在ledger485.9093s及1689.7142s觸發，4.6164s／5.3864s後各1IOC成功flat；不是hold退出。Strict來源11664包、最大age337.9858ms、0超界；REST／WS／TX峰10100／102／9，無API拒絕退出。

**根因的證據界線。** 本場此前38次正常撤換操作已成功取消43個ID，67份OrderEvidence的ID皆不同；最後BUY原price77409.4／amount0.00040與13份healthy working快照一致。沒有上輪每次正常撤單都被占位DTO拒絕、ID重用或價量改寫的證據。但舊 `cancel_nonterminal_response` 同時涵蓋真正缺terminal與strict欄位不合格，journal未保存該receipt／poll結果，因此不能確證最初是history延遲、provider錯誤或欄位矛盾。SDK會把簽名前失敗與send_tx的部分異常壓成相同err tuple，不能據此聲稱未送出或放行重送。

**已重現並修復的收尾限制。** 舊恢復操作只sync一次；第一次仍active即消耗資格，縱使0.5s後exact terminal到達、原10s取證期限仍有9.5s，也沒有再讀入口。現在同一次操作仍先消耗資格，最多兩次fresh sync，第二次前等0.5s；仍受原 `min(cleanup_deadline, now+10s)`、fault／mutation generation、known IDs、無unknown／外來registry限制，每次讀取都走原admission。只有缺原pending terminal且其餘安全條件仍成立才可第二讀；額度拒絕、期限不足或新故障就停止。Exact terminal及registry確認後才接原fresh account、最多3IOC／固定價格／總30s收尾；不重送不確定撤單、不恢復正常報價、不提高API／風險限額。第二次可選讀取的額度拒絕另記secondary原因，不蓋掉最初撤單失敗。這是可重現的liveness修正，不能證明本場224839在這10s內必定會出現終態。

**撤單診斷補齊。** MM opt-in的共用REST按當次已知ID最多保存兩筆固定submission類別及history attempts／read errors／exact matches／captured terminal計數；不保留raw provider錯誤、receipt或憑證，confirm後清除，Grid/default未啟用。Manager與strict驗證共用同一判定，分開PENDING／缺receipt與symbol／side／type／exchange ID／client／financial／amount／price／remaining拒絕原因；在確認清理前擷取到該cancel action。Telemetry只接當前pending ID與相符失敗cancel的allowlist資料，不借用舊單或另一操作；保留舊總旗標相容性。Recovery失敗另記讀取次數、未證明ID數、剩餘時間及固定原因。同步診斷getter拋錯或自拋CancelledError均隔離，真正交易await的task cancellation仍傳遞。

Analyzer以前在incomplete replay用ledger.snapshot預設false/null覆蓋已記錄final facts；現在保留typed report的final authentication／position／count，並另列最後AccountSnapshot及兩者行號。來源矛盾或缺失照實保留，沒有重新認證或升級economics。本場重算 `.jsonl.analysis.json` 正確呈現true／−0.00040／1，formal淨值及fee-cover保持null。

**驗證。** 真Adapter→REST→manager→session整合重現4次history皆無終態、對側SELL成交、第一次exit查核仍active、第二次partial CANCELED到達，再以fresh residual作單次IOC→authenticated0/0（離線fixture原量0.1、partial0.04、殘量0.06，非本次實盤交易）。正常maker只原兩張，不重送cancel，原deadline只建立一次；failed run仍failed。實際API成本fixture另驗第二次讀取計費與原1000REST／5WS準入，缺額度時不讀、不加交易。原persistent active／absence、generic fault／unknown、partial IOC及長窗口回歸保留。

**最終驗證於2026-09-13完成：全專案901 tests／101.791s，613項V2全數通過。** Repository `.venv`執行 `unittest discover -v -s tests -p "test_*.py"`，完整結果 `logs/mm_v2_224839_final_verbose_tests.log`。8 failures＋4 errors與 `logs/mm_v2_phase7_20260904/v2_only_cleanup_full_tests.log` 的12個既有Grid／Lighter cancellation方法逐一相同，無新增失敗，並非宣稱全專案全綠。第一輪完整suite在沒有結果摘要時中止，沒有當作驗收，故以逐項輸出重跑取得本次完整證據。Shared default、前批修復及固定10／30／60min離線工作量皆包含；獨立審查及 `git diff --check` 完成。

本批未調quote／size／loss／inventory／fee／source參數，未修改Grid策略、配置或測試；此前未提交變更保留，沒有commit/push或VPS操作。只有上述一次授權唯讀連線；修後完整60分鐘live、最初撤單provider原因及原場後續歸零成本仍待實際證據。

## 2026-09-12 場次 215907：修正完整撤單終態在共用轉接層遺失

使用者提供 `logs/mm_v2_economics_20260912_215907_056.jsonl` 的退出畫面後，核對同場 journal、window、budget 與重新產生的 `.jsonl.analysis.json`。Journal SHA256 `1887cd403b30fedf5108471fe24b7c089a75a718dbe223a4bde1eca8ddf4a679`；startup provenance為 `b4605b9`、dirty=true，含前批未提交修改。Planned3600s、wall128.5786228s、ledger65.6977842s後code1；0 fills／0 fees，首次60s quote expiry時 `reconciling_quotes` 報ExecutionUnavailable及下游ValueError。兩則診斷均有 `cancel_nonterminal_response=1`，buy uncertain_cancellation、sell live。

**本場有成功收尾，整場仍失敗。** 原退出路徑取得晚到的確切終態、撤餘單後，cleanup与final均authenticated position/orders0/0；IOC attempts0。最後ledger會計完整，但runtime failure使formal economics維持unavailable，不能把沒有成交、沒有費用當成fee-cover通過。取消前最近診斷到cleanup flat為3.2764433s；不是已量測的wire cancellation耗時。`sendTx`共4次（2create／2cancel）、inactive-order reads7次，無API／account背壓或optional deferral。397份strict source記錄皆有效。本批未新連線或啟動live。

**可重現的程式根因。** 上一批對manager取消終態加入原size／price／side／exact ID檢查，卻未走真實共用Adapter→REST的正常撤單交接。REST當時只為FILLED保留完整OrderData；CANCELED／REJECTED／EXPIRED則清掉registry後回bool。Adapter再重建amount0、price=None、side BUY的占位回覆，連有效取消也不符合manager契約；正確的history DTO已丟失。原測試fake直接回傳完整OrderData，漏掉這個整合缺口。新增真Adapter→REST→manager測試在修復前，BUY／SELL×五種終態的10個子案例中，8個非FILLED失敗、2個FILLED通過，證明交接不對稱。歷史journal未保存cancel receipt，故不能聲稱日誌直接證實某個price／ID欄位錯配；程式重現與本場首次到期故障相符。

**修復。** 僅在既有MM opt-in下，REST保留CANCELED、partial CANCELED、FILLED、EXPIRED、REJECTED的原始完整DTO，Adapter直接交給consumer；registry／cache在manager完成原exact terminal檢查後才清除。尚未確認的再次呼叫只讀取終態，已有cache就直接交接，不重送signer、不新增HTTP、不延長輪詢或退出期限。仍active、absence、錯ID／symbol／size／price／side等不構成取消成功，保留前批有界取證及fail-closed。Grid/default未啟用此契約，原bool／placeholder與client-alias行為保持。

獨立審查另發現同一交接生命週期內的編號碰撞：一張合法terminal的client ID可能等於另一張pending的exchange ID；舊Adapter confirmer會兩個都清除。MM confirmer現在僅確認原exchange ID，client-only／空ID不消費registry；兩張同status pending的marker／cache對照測試確認另一張保留，default client-alias行為另有回歸。這是可重現的潛在缺口，不宣稱本場曾發生。

**驗證。** 真Adapter→REST→manager涵蓋雙向正常／部分取消、成交競速、expired／rejected、晚到或一直缺terminal、cached receipt不重送及default對照。新增完整session回歸以假時鐘跑125s，經兩輪60s雙向quote expiry後繼續POST_ONLY，最後fresh authenticated0/0；每張只cancel一次、每次成功取消只讀一份history，無額外active read，沒有借exit recovery把正常報價失敗掩成通過。40項Lighter execution及63項session runner測試先行通過，獨立安全審查無剩餘阻擋問題。

**最終完整suite：889 tests／98.161s，601項V2全數通過。** 使用repository `.venv`／`unittest discover -s tests -p "test_*.py"`，結果保存 `logs/mm_v2_215907_final_tests.log`。8 failures＋4 errors與 `logs/mm_v2_phase7_20260904/v2_only_cleanup_full_tests.log` 的12個既有Grid／Lighter cancellation方法逐一相同，無新增失敗；不是全專案全綠。Shared defaults、read-stream observer、前批資金費／API／exact accounting／部分IOC／stop與deadline、固定10／30／60min離線負載均包含於本次full suite；`git diff --check`通過。沒有為確認相同結果再跑重複V2 suite。

本批未改報價、size、風險、API額度或退出參數，未修改Grid策略／config／tests；之前未提交工作保留。未啟動新live／私人查核／VPS或commit/push。完整60分鐘實盤與經濟表現仍待修後場次；本地回歸不是實盤驗收。

## 2026-09-12 場次 205914：已知撤單不確定阻斷收尾，補全退出取證

使用者要求「分析我最新的測試紀錄，並且優化」。本場為 `logs/mm_v2_economics_20260912_205914_430.jsonl`，7173 events，SHA256 `b0e5d44cd214fce91ab1bd5975108fa5e1bd08e848aae6de0f6e3d1f037ca441`。Startup provenance明示 `b4605b9`、dirty=true，包含前批尚未提交的修正，不能視為該commit的乾淨版本。實際設定已有證據：size0.00040、edge0.20bps、volatility multiplier1.0、skew2.0bps、soft/hard0.00040/0.00080、max hold180s、loss0.50、stop0.15、IOC200ticks；本批未調這些值。

**原輪失敗與後續觀測分開。** Planned3600s，wall1751.6822554s（29分12秒）、ledger1685.3013526s（28分05秒），exit code1。最後strict account仍short0.00040／2張委託，第三次退出blocked、IOC attempts0；原輪沒有自動收尾0/0證明。使用者確認沒有手動處理，並另外授權一次同帳戶Robinhood主網唯讀查核：21:43:28 +08觀測全帳戶positions=[]、open orders=[]，身份驗證通過、已斷線，create/cancel皆0。這是兩次相隔約126ms的順序讀取，並非原run的atomic final proof；歸零過程、後續成交與成本尚未取證，不回填原run成功。結果保存在同stem `.readonly_postflight.json`；此次唯讀授權已完成，不代表新交易授權。

| 本場已記錄結果 | 數值 |
|---|---:|
| Maker fills／turnover | 25／680.019960 USDG |
| IOC fills／turnover | 2／30.915860 USDG |
| Realized gross／maker fee／taker fee | 0.017700／0.08160239520／0.01082055100 USDG |
| 已記錄交易net／最後marked net | −0.07472294620／−0.07940294620 USDG |
| 有單／雙邊秒 | 1603.4634884／1529.1574765 |
| 有單／雙邊占ledger時間 | 95.14%／90.74% |
| API／可恢復account背壓退出 | 0／0 |

10組完整flat-to-flat：8組maker-only gross0.028440、fee0.05934674400、net−0.03090674400；7組gross正，僅2組net正，5組正價差不足付費。2組含IOC net−0.04010926780；最後未閉合short另有已記錄fee0.00370693440。兩次正常IOC皆由inventory_hold觸發，27筆fills全有order證據，25筆maker皆normal、passive reducing為0。診斷gross/fee19.15%，但原輪尚有未平倉損益與之後退出成本；formal economics仍unavailable，不能直接與旧輪3.12%比較為盈利改善。

相較231145，有單／雙邊覆蓋82.93%／54.98%→95.14%／90.74%；maker/實際wall-hour2883.87→1397.55、maker/quote-hour3554.33→1526.74。不同時段及未完成窗口，不是控制實驗。新4343筆public BBO支持1s／5s maker markout：覆蓋17/25（68%）與19/25（76%），turnover加權分別−0.443712／−0.304824bps、均未扣每側1.2bps maker fee；缺配對為first-after sample超過250ms容差，不插值、不回填fill reference。舊輪無此public evidence，不能同口徑比較。維持先處理執行與成本證據，不盲目增加edge或成交量。

**故障鏈。** 最後正常報價改價取消一側，manager留下buy live／sell uncertain_cancellation；`execution_port`把缺terminal取消標為全域halt。之後orchestrator的「execution blocked」只是下游錯誤，真正收尾在exit_health就被HALTED擋下。當時無API/backpressure退出，REST/WS/TX峰9200/98/9；12404筆strict來源最大age332.9746ms、0超界。最初cancel未取得terminal的具體原因缺少provider分類，不宣稱已證實是transport、signer拒絕或history延遲。

**本地修正。** Known-cancel失敗保留該次確定owned ID；只有沒有未知委託、提交疑慮或外來取消registry時，才可在原cleanup deadline內做一次exit-only sync（單次至多10s，照原retry admission及實際API observer計費）。同ID仍active、缺席無terminal、錯ID、generation改變、來源/查核失敗，都不能清除halt或重送撤單。須原pending IDs全取得exact terminal、registry清空且剩餘known slots健康，才清除此取消專屬鎖停，接回撤餘單→fresh account→原固定價格／最多3IOC／原30s退出。通用snapshot/IOC/未知提交halt不可經此流程解除，正常報價不因收尾恢復而重啟；失敗run仍為失敗。

同一路徑也覆蓋首次bounded cancel才遇到延遲terminal的情形：在原10s取消期限內最多一次補查；證明後只取消尚未處理的已知另一側，不重送不確定單。失敗未獲證明就停止；不重置期限或額外贈送IOC attempts。正常quoted path、bounded cancellation及實際API計費都有回歸，含partial fill後按fresh residual減量。

獨立審查另外重現兩個安全邊界：sync期間新generic halt可能被舊取消恢复覆寫，以及同client ID／另一個已known exchange ID的terminal可能被記到原slot。新增fault generation，取消或補查期間的新generic fault都不能被降級或清除；取消終態要求exchange ID精確一致、已有client ID不矛盾、原size／price一致及remaining不增加，涵蓋直接回覆、cached terminal、history與取消中的order update。這些是允許新恢復入口前必須補上的證據邊界，不代表本場已觀測到身份錯配。

另外補上取消batch的固定原因flags與pending/matched counts，僅採用目前不確定slot與相符cancel action的本地結果；未知或過期原因標unknown，不保存原始provider錯誤。修正分析器錯讀sidecar路徑（真runner為 `.jsonl.budget.json`），加入真runner寫入→analyzer CLI讀取的離線契約測試，本場provenance現在正確available；分析保存在同stem `.jsonl.analysis.json`。

**最終595項V2 tests PASS（86.813s）**，repository `.venv`／`unittest discover -s tests -p "test_mm_v2_*.py"`；`git diff --check`與獨立安全審查通過。包含真normal reprice取消未確認→已知單補查→partial fill→fresh residual IOC→final0/0，以及still-open／absence負例、補查API observer與原retry allowance、取消和sync期間的新generic halt；取消proof另覆蓋4個來源×14種契約情況。舊資金費有界恢復、cash cache、API背壓、固定10／30／60min負載、部分IOC及stop／deadline回歸亦通過。嚴格terminal檢查揭露的本地fake receipt已改成原slot的真實size／price／client，不降低安全斷言。

本批新production修改限V2，前批未提交的shared observer等修改保留、其已完成完整870測試的證據不重寫；沒有新Grid／shared adapter／local live YAML／quote／risk參數修改。只完成上述一次授權唯讀查核，未啟動新live、委託操作、VPS或commit/push。修後完整實盤60分鐘及fee-cover仍待新證據，595項本地PASS不代表實盤不會再失敗。

## 2026-09-12 場次 231145 fee-cover 分析後：減倉容量一致性與成本歸因

依使用者「請依分析進行下一步優化」完成本地修改，起點為 `refactor/lighter-volume-mm-v2`／`b4605b9`。下載報告及貼文作為待核對分析，沒有用來授權新實盤、增加風險或指定參數。上一批 funding／監控／缺側補單修復保留；歷史231145不當作修後運行結果。

**歷史重算。** 原journal共3359個事件，SHA256為 `972d0c0498746f0b3ea76f41676b64c9470c44d59655989003ff40534e4d644e`。既有分析器現在按真實持倉從0到再次精確0分組，保留整筆翻倉及部分成交，不拆配勝利交易、不引入runtime episode。23組皆完整：19組maker-only的gross／fee／交易net為 `0.008163 / 0.14018300124 / -0.13202000124`；4組含taker為 `0.002376 / 0.19762771008 / -0.19525171008`。Maker-only有12組gross正、5組net正，7組正gross不足付費；合計精確重現90 fills、gross `0.010539`、fee `0.33781071132`、交易net `-0.32727171132`。交易gross／fee約3.12%只作診斷，正式fee-cover仍因final accounting不完整而不可用；不回填候選funding。

**重現並修正容量缺口。** 原governor對兩側共用lot搜尋；非flat時，加倉側的風險預留可把兩側一起壓到minimum以下，連原本可負擔的正常減倉報價也消失，因而進入passive-touch。現在僅在原搜尋兩側皆不可下單時，再以同一風險公式搜尋真正減倉側，上限為 `min(abs(position), order_size)`；仍包含全部未撤舊單、working gap、stop／taker／slippage／maker reserve與嚴格 `< max_loss`。符合minimum才保留正常單側報價；不翻倉、不提高loss或inventory限額、不延長hold／deadline、不以獲利條件阻止退出。真正reserve不足及hard／hold／loss／stop退出維持原路徑。

可重現合成情境：order `0.00040`、minimum `0.00020`、soft／hard `0.00040 / 0.00080`、session loss `0.5`、stop reserve基準 `0.15`、maker／taker fee `0.00012 / 0.00035`、slippage 200 ticks × 0.1。先有交易loss `0.28`，正常BUY `0.00040 @ 77103.9`成交後，fresh BBO為 `77103.9 / 77104.0`，inventory loss為0、current loss `0.28370098720`。

| 同一合成快照 | 原共用容量搜尋 | 修正後 |
|---|---|---|
| 可執行容量 | 兩側各0.00010，均低於minimum | BUY 0／SELL 0.00040 |
| 報價路徑 | REDUCE_ONLY，SELL @77104.0 | SKEWED，正常SELL @77113.3 |
| 同風險公式 | 未搜尋獨立減倉候選 | current loss＋reserve `0.45619933920 < 0.5` |

保留原安全對側單時仍計入舊價，合計為 `0.45620283600 < 0.5`。多空、部分持倉、subminimum、未撤加倉單及嚴格邊界皆有回歸。這是程式缺口的受控重現，不能唯一解釋缺少governor原因的歷史每筆快速減倉，也不預測新價格會成交或獲利。

**下一輪能留下的證據。** 新 `GovernorDiagnostic` 記錄實際轉態原因、minimum、候選與核准容量，以及當次真實風險計算的各預留項；未經該計算的分支保持null，`remaining_loss_headroom`是扣current loss後、尚未扣reserve的餘額。`OrderEvidence`在owned order ID確認後、對應fills進帳前記錄實際提交的side／price／size／reduce-only／TIF、當時external market及本地提交／確認時間；保留單不重置原時間。IOC仍須原exact terminal證明。後續恢復才發現的未知提交可能無此證據，分析器維持unclassified，不補造確認事件。

已驗證公開stream以最多4Hz抽樣BBO、原source timestamp／nonce／receipt time／size；不新增請求或訂閱，Grid預設observer為None、原REST及行情有效性規則不變。Journal先有初始AccountSnapshot，再註冊observer；final report前停止接收診斷。Callback故障不阻斷known-order清理。公開BBO可能含自己的單，只供成交後1s／5s source-clock markout，須目標時間後0.25s內樣本且列明覆蓋率；不宣稱external fill-time reference或歷史wall-clock age已驗證。本地submit→fill receipt是觀測區間，不是假定的交易所實際掛單年齡。

既有 `analyze_mm_v2_session.py` 新增normal maker／passive reducing maker／IOC／unclassified成本、分組、governor原因及上述時間／markout覆蓋。類別gross採平倉fill認列，不能當作獨立策略收益或刪除退出後的反事實。原90筆fill的reference仍全null，spread capture／inventory decomposition／flatten concession缺證據仍不可用，不用後來quote或稀疏mark回填。Runner在adapter啟動前將當次commit／dirty／allowlisted有效quote、inventory、flatten、session設定寫入原budget sidecar，啟動失敗亦保存；無Git就明示unavailable，無帳戶、金鑰、路徑或exchange配置。舊場次缺此證據時不拿目前HEAD或schema example補猜。

**最終驗證：全專案870 tests／91.838s，582項V2全數通過。** 其餘8 failures＋4 errors與 `logs/mm_v2_phase7_20260904/v2_only_cleanup_full_tests.log` 的12項既有Grid／Lighter cancellation基線逐一相同，無新增失敗；不是宣稱全專案全綠。共用read-stream預設、callback隔離與無額外API測試包含在本次完整suite。首次完整測試另外揭露兩個舊Buffered fixture使用float毫秒時間，與真WS `_integer` 契約不符；僅修正三處fixture為int，原immutable-book watermark及第二次IOC handoff安全斷言保留，再完成上述統一重跑。`git diff --check`與獨立order safety審查通過。

固定600／1800／3600s離線回歸仍涵蓋funding race、known-order／unknown mutation、partial IOC、account cache、monitor預留及stop／deadline。最終一小時fixture為3600.106s、42 maker／8 taker、0 API退出、cash差0／final0/0，雙邊3281.967s（約91.16%）；離線模擬仍保留IOC成本及延遲場景錯過的成交，不把unit PASS當實盤成本改善。新CLI另將歷史重算存於同stem `.fee_cover.analysis.json`，明示90筆fill／23組、order／governor／公開樣本coverage為0、formal economics=false。

本批未改報價參數、策略fee floor或風險限額，未啟動新live／私人帳戶連線／VPS，也未使用已完成的一次性commit/push授權。實盤完整60分鐘、最終funding對帳及fee-cover仍待新場次證據；本地容量修正與診斷完整不代表經濟達標。

## 2026-09-11 場次 231145 後：資金費有界恢復、空委託監控成本與缺側補單

依使用者「ok請就這幾點先優化」完成 V2 本地修復。下面231145的歷史失敗、現金差額、實際54.98%雙邊覆蓋及未完成60分鐘的結論保留；本批沒有新live、私人帳戶連線、Grid／shared adapter／risk或quote配置修改、VPS、commit/push。公開funding等額吻合仍只是上輪歸因證據，未補造或回填私人funding ID。

**資金費時序與收尾。** 正常audit仍只有原2次／10s；僅精確cash bridge的 `UnattributedCashflow` 可進新流程，費率不符、身份錯誤、unknown mutation等不混入。先在原期限完成known-order撤單與bounded exit，取得fresh authenticated0/0，再最多2份strict snapshot，中間1s等待，同一10s且不超過原cleanup剩餘期限。Funding checkpoint取於失敗audit之前，覆蓋「cash先到」及「私人ID先到」兩種時序。需新authenticated ID及逐位exactcash一致才能恢復同一ledger、loss limits與session deadline；沒有ID而cash自行回正、錯金額、長期缺紀錄均停止。Stop/deadline後只完成帳務，不重報價。失敗不在finally重新啟動取證窗口。

Final-account才發現缺funding也可於原final10s內取證。新funding入帳時間晚於該次opening cash，故成功後另取一次fresh confirmation；同期限、同逐讀成本檢查，不重蓋舊cash時間、不遞迴延長。這修正了「funding已精確入帳／final0/0，但ledger最後證明仍早於事件」的實際離線重現。

**只在已清理0/0後調整讀取預留。** 真端點權重fixture先證明：已清理0/0、REST用量9800時，額外audit300仍被普通「再預留完整3IOC」公式在未來24s以24306>24000拒絕。新 `require_flat_read` 只用於上述同健康stream、固定mutation generation、禁止mutation的取證範圍；每個read仍由實際observer計費，額外保留最後一次strict proof的4400 REST／15 WS／0 TX。單份proof最多2attempt，每attempt cash300＋fees/public funding900＋settlement300＋trades600＋terminal100=2200 REST、5 WS，另留5控制幀。4400是最後查核預留，**不是整段recovery總成本上限**；額外probes會累計並可能被拒絕。普通quote/create/cancel預留、24000／200／40限額均未改；重入先經原cooldown及完整風控／admission。stream遺失或更換、generation改變、未知委託會在下一read前中止，禁止REST fallback使用此較窄成本契約。

**監控與雙邊。** 上輪兩個API monitor退出之前皆已authenticated無單、只剩±0.00060持倉。只有completed fresh audit、同generation、所有known terminal fills已證明、account／manager／healthy execution都空時，下一monitor才從1200降為cash300，再加原due terms；有單或證據不全仍原1200，所有實際conditional read gates不變。同6100 REST burst合成契約中，monitor2400以24206拒絕，monitor1500通過；不能把此合成成本檢查當成缺失逐請求時序的完整實盤重播。

執行層若只剩一張安全委託，且它的真實價格可與最新缺側intent共存，先補缺側，再考慮可選改價。原ID、價格、剩餘量與排隊時間保留；必要撤单、每筆create的fresh風控／API gate、POST_ONLY、自成交交叉／鎖價拒絕、原10s deadline均不變。回歸覆蓋多空對稱、create被拒、低於新單minimum的合法既有partial、過期或不再被風控允許的委託。

**完整V2 suite：561 tests PASS，86.360s。** 使用repository `.venv` 與 `unittest discover -s tests -p "test_mm_v2_*.py"`，包含本次及以往cash cache、terminal／unknown mutation、partial IOC、固定價格／風控、stop／deadline等回歸。未改shared production／Grid，未額外重跑無關全專案基線。180s真端點權重離線fixture證明晚到funding只入帳一次、精確cash差0、final0/0、原窗口運行完畢，保留普通報價cooldown後重入。

| 相同離線工作量，本批結果 | 完整ledger秒 | Maker機會／fills | IOC／taker fills | API退出 | REST權重／TX |
|---|---:|---:|---:|---:|---:|
| 固定10min | 600.106 | 15/15機會 | 5 | 0 | 69106／59 |
| 確認延遲／REST放大10min | 600.106 | 14/15，保留1次錯過 | 4 | 0 | 70106／62 |
| 固定30min | 1800.106 | 45/45機會 | 15 | 0 | 202406／177 |
| 連續60min | 3600.106 | 42 maker fills | 8 | 0 | 351406／320 |

連續60min actual quote3376.653s／two-sided3282.337s，雙邊占ledger **91.17%**；上一批同fixture為3247.132s／90.20%。Funding−0.00038400912只入帳一次／public round只讀一次，cash差0、final0/0、六個10min窗口皆有報價；REST/WS/TX峰值10000／118／13，原限額24000／200／40。30min固定機會從上一批44/45變45/45；API退出均0，IOC15不變，REST200906→202406、TX169→177。60min REST351306→351406、TX316→320，不能宣稱總请求量或費用因此降低。舊disabled-optional對照仍在約259s第三次背壓早停，未藉調整fixture移除失敗證據。

這些是本地執行／會計證據，不是修後實盤60分鐘、雙邊覆蓋或fee-cover驗收。沒有用網路flat dry冒充funding／partial-fill驗證；新live仍需該次明確授權，未自動啟動。

## 2026-09-11 場次 231145：跨整點資金費高度吻合現金差額，一小時實盤仍未完成（分析）

最新使用者場次 `logs/mm_v2_economics_20260910_231145_705.jsonl`，window為planned3600s、wall2901.2834956s（48分21秒）、exit code1；ledger2838.4683620s（47分18秒）。不是完整60分鐘。現有analyzer獨立重播沒有 `recorded_events_do_not_reconcile_to_report`，但明確為 `incomplete_final_accounting` 與 `runtime_failure_diagnostic`、economics_evaluated=false；同stem `.jsonl.analysis.json` 保存結果。帳本 `failed=false` 欄位不可取代程序code1與兩筆failure diagnostic。

**最後失敗為 cash bridge，不是第三次API背壓早停。** `authorizing_quotes`及`final_account`皆為 `_AccountCashRace`：expected equity297.260512415252、account equity297.260873946188，交易所現金多出 **0.000361530936 USDG**；execution healthy、managed states空、uncertain/unknown皆false。Exit-5在ledger t2837.0618712s取得fresh authenticated position/orders0/0、attempts0，當時本已flat，沒有多送IOC。其後最後嚴格財務查核仍失敗；不能把清理0/0、帳本position0或report.failed=false當作整輪成功。此為歷史最後清理快照，本次分析未重新連接帳戶。

**公開資金費與差額精確相符，私人歷史發布延遲尚未證實。** 最後SELL0.00039@77210發生於2026-09-10 23:59:49.494 +08，BUY0.00039@77210.5於2026-09-11 00:00:01.480 +08；依整輪signed fills，整點持有short0.00039。公開[Robinhood BTC funding round](https://api.rh.lighter.xyz/api/v1/fundings?market_id=1&resolution=1h&start_timestamp=1789052400&end_timestamp=1789056001&count_back=3)的timestamp1789056000、unit value0.92700240、rate0.0012%、direction long。按現有exact-funding公式，short應收 `0.92700240 × 0.00039 = 0.000361530936`，恰等於現金差額。這強烈支持資金費已進現金而尚未進本場ledger；不能由金額相同就補造authenticated funding ID。

本場ledger funding0、positionFunding/accountLimits各96次，而owned observer沒有任何 `rest:fundings` request。程式只有在私人positionFunding返回新ID後才查public round；正常cash mismatch的一次forced refresh及final讀取仍未使該筆入帳。故優先查核的是整點cash／private funding history的可見時序，尚不能斷言是交易所延遲、資料窗口、還是程式對新row的發現缺口。公開round、boundary持倉與等額差異保存於同stem `.funding_evidence.json`；只有公開無認證查詢，未查私人funding紀錄。未放寬精確比較、回填原journal或修改runtime。

| 本場觀測 | 數值 |
|---|---:|
| Maker turnover／fills／不同委託 | 2324.147061 USDG／85／80 |
| Maker買／賣額 | 1199.852661／1124.294400 USDG |
| Taker turnover／fills | 168.323040 USDG／5 |
| 已記錄交易gross | +0.010539 USDG |
| Maker fee／taker fee／總費用 | 0.27889764732／0.05891306400／0.33781071132 USDG |
| 成交ledger net（funding未入帳） | −0.32727171132 USDG |
| 清理快照相對起始現金變動 | −0.326910180384 USDG |
| 實際有單／雙邊秒 | 2354.0084185／1560.4745028 |
| 有單／雙邊占ledger窗口 | 82.93%／54.98% |

交易gross只覆蓋總費用3.12%，尚未fee-cover；本場不是靠較大的maker總額就達標。按原3600s窗口，maker額2324.147061 USDG；按實際含startup的wall折算約2883.87 USDG/h，兩種分母須分開。成交ledger cost約1.40814bps（未含缺失funding），不是原run已驗證all-in。清理快照現金變動與候選funding調整後net相等，仍不覆寫失敗的最後會計結果。

| Ledger分鐘 | Maker USDG／fills | Taker fills | 交易gross減fee USDG | 有單／雙邊秒 |
|---|---:|---:|---:|---:|
| 0–10 | 415.593864／17 | 1 | −0.05222291568 | 548.592／372.586 |
| 10–20 | 585.987540／23 | 0 | −0.06444850480 | 575.386／377.315 |
| 20–30 | 478.615400／17 | 3 | −0.09106091400 | 509.138／282.960 |
| 30–40 | 478.244940／16 | 1 | −0.08729773880 | 411.854／279.318 |
| 40–47:18 | 365.705317／12 | 0 | −0.03224163804 | 309.038／248.295 |
| 47:18–60 | 未運行 | 0 | 不外推 | 0／0 |

**運作中斷减少，但API與雙邊持續性仍未完成實盤驗收。** 可恢復account read deferrals0、optional_waits128；兩次正常風控退出、兩次API monitor退出，最後一次cash failure後的flat清理，共5次。API兩次在t約1529.381及1834.529s，均REST monitor2400，未來24／32s預計24806／24206>24000，仍為本地完整退出預留拒絕，不是已證實429；每次清理後恢復，未達第三次API早停條件。近期denial只保留最後64筆，不拿該長度當整場拒絕總數。34467筆strict行情、最大age334.3102ms、無source例外；不能將本場現金故障歸因於行情延遲。

與上輪221113比較相同前862.8702873s：退出6→1（舊3API＋3account、新1風控），API3→0、可恢復account race3→0；有單612.998→804.692s，雙邊519.164→521.875s，maker額859.357890→646.968044（−24.71%）、fills33→25，taker fills5→1，交易net−0.26045506055→−0.09046681728。不同真實市場/成交条件，不是控制實驗；可以說中斷與taker負擔減少，不能說相同時間量能或雙邊覆蓋已提升。完整本場雙邊54.98%，也未重現離線一小時90.20%。

本次只完成歷史分析、公開funding核對與既有文件狀態更新；沒有新live、私人帳戶連線、策略／risk／Grid／VPS修改、commit/push，也不為分析文件重跑產品測試。上一批542 V2 PASS仍是既有本地證據。後續優先範圍為跨整點資金費可見時序與有界恢復／最終對帳，其次是剩餘monitor背壓與單側報價；實盤完整60分鐘及fee-cover皆未通過。

## 2026-09-10 場次 221113：實盤早停歸因與持續運行缺口修復（本地完成）

使用者提供的 `logs/mm_v2_economics_20260910_221113_386.jsonl` 原定 3600s，實際 ledger **862.8702873s**／完整 wall **925.7702803s**，以 `api_backpressure_repeated`、code 0 提早停止。`complete=true`／`failed=false` 只證明本場帳務及收尾完整，不能當成完成一小時。33 maker fills／859.357890 USDG、5 taker fills／88.620325 USDG；gross −0.126315、maker fee 0.10312294680、taker fee 0.03101711375、funding 0、all-in net **−0.26045506055 USDG**、cost 3.03081014 bps。Final authenticated position/orders 為 **0/0**、exact cash bridge 差額 0；這是該場最後觀測，沒有本批重新連接帳戶查核。實際有单 612.9975186s、雙邊 519.1636819s；早停仍保留原 3600s 比較分母。

**六次收尾分屬三次 API 背壓與三次 account read race，不是六次 API 拒絕。** Sidecar 另有 8 次成功可選延後，與退出分開計數。API 三次均在 `reconciling_quotes`，可選 create/reprice 已被拒絕，接著必要下一輪 `monitor` 亦無完整退出預留：

| 退出 | 當前 REST 用量 | 下一監控 REST | 首個未來阻擋點 | 預計 REST／上限 |
|---|---:|---:|---:|---:|
| exit-1 | 9600 | 2400 | 24s | 24106／24000 |
| exit-4 | 10600 | 1200 | 16s | 24206／24000 |
| exit-6 | 10700 | 1200 | 16.5s | 24006／24000 |

三次均為本地 `rest` 預留拒絕，沒有證據顯示交易所 429。不能只以當前 REST 或峰值低於 24000 就判定監控一定能繼續。REST／WS／TX 峰值為 13300／122／11；startup／normal／exit REST 為 2700／97606／18000。13370 筆行情封包皆在 strict 範圍，最大 accepted age 386.9598000ms、零 source 例外，沒有證據將本輪早停歸因於行情延遲。126 個 quote execution results 中 84 個為零送零撤 confirmed、8 個為零送零撤 deferred，其餘為單側補單或必要撤換；不能說所有循環都在整對撤建。

Account read race 對應 exit-2／3／5：各次最後完成結果之後都有新的 `quote_plan`，但沒有新 `execution_result` 就進入收尾；結合 runner 的唯一 plan emit 路徑，可定位同為 `reconciling_quotes` 內的重新查核。附近確有 maker 成交，包括同單 0.00028＋0.00012 的 partial fills，但舊 sidecar 沒有記錄這三次的錯誤子項，**不能唯一認定是 cache、bracket、counter、history 或 position 哪一項失配**。六次退出皆有新 authenticated 0/0，五次需 IOC、exit-5 原已 flat；本輪沒有復發以 maker minimum 阻擋小殘量 IOC、固定 limit 零成交鎖停或循環小數阻斷 cash/exit 的舊症狀。

本批從同一路徑的成本／證據生命週期修復：create 預檢納入後續監控，為 REST 2600＋15s 內到期 terms／WS13／TX2；可選 selected-side reprice 為 REST `412 × sides + 3800`＋15s 內到期 terms／WS18／TX `sides + 2`。原各階段 admission、下一輪 monitor gate 與完整 scheduled exit reserve 仍保留，並非先檢查後跳過真實 read／mutation 邊界。只有一側必須撤除時，安全另一側的可選改價仍須先預檢，拒絕不得把它夾帶到必要取消中。延後 wake 使用 ledger age 與既有 cash 觀測保守持倉年齡的較大值，不以延後重置 max hold。

Unified cash cache 若因新成交、counter 或 exact state 不再可重用，於原 10s audit deadline 內走重新 admission 的 fresh REST／完整 bracket；cache miss 不直接當作兩次真實查核失敗。Fresh proof 仍不一致、cash conflict、unknown execution、stale book／account 或必要 API 拒絕仍 fail closed，不增加 retry／TTL 或放寬精確帳務。新增 bounded `recent_account_read_exits`，保留最多 64 筆 allowlisted phase／exit ID／時間／subreason，與 API exit attribution 分開；不保存 exception payload 或帳戶身份。Cache 失效的可重現本地缺陷與歷史未分類 race 保持區分。

Flat/empty 的無 REST 等候恢復目標同步改為 REST5000／WS18／TX2，涵蓋重新查核、到期 terms、第一筆 create 及後續 monitor，避免只達舊門檻就反覆進場查帳。這是恢復目標而非持有 reservation，所有實際 gates 仍執行。Cash cache fallback 丟棄舊 opening/closing handoff，重新取得完整邊界；最壞正常路徑為三段查核、15 WS frames、最多兩次 fresh REST balances，各段開始前有 audit admission。它仍受同一10s deadline及一次真正 fresh race retry限制，exit/final從不走 cash reuse，退出 reserve不變。Closing之後、account_all回來前若 mutation generation改變，立即拒絕，不得藉fallback掩蓋。

**最終 542 項 V2 tests PASS（90.506s）**，使用 repository `.venv`／`unittest discover -s tests -p "test_mm_v2_*.py"`；`git diff --check`及獨立契約檢視通過。新增重現涵蓋 create不能花掉下一次監控、雙方向混合必要／可選撤單、cache失效再遇真正race、persistent mismatch、額外讀取預算拒絕、原10s／generation限制，以及延遲58s才發現成交時仍依保守hold期限喚醒。既有部分成交精確realized cash、小殘量與固定限價IOC、funding、未知mutation／known-order清理與stop/deadline回歸皆包含於此套件。

| 最終離線場景 | 完整時長 | 固定 maker 機會 | API 強制退出 | REST 權重／TX | IOC |
|---|---:|---:|---:|---:|---:|
| 集中成交 | 600.106s | 15／15 | 0 | 68206／57 | 5 |
| 確認延遲放大 | 600.105s | 14／15，錯過1次 | 0 | 69506／59 | 4 |
| 連續集中成交 | 1800.105s | 44／45，錯過1次 | 0 | 200906／169 | 15 |

三個固定機會場景沒有補排錯過事件，皆到原deadline、exact cash差0及final authenticated0/0。成本fixture補上首次nonce6、首次及每300s market details300；放大情境真實執行三次orders確認（6 WS frames）及一次terminal history，耗時1.811s；另一次撤單在第四次history讀取才取得terminal，耗時1.505s。未計入的其他SDK retries、外部API消費與真實來源延遲仍是限制。早先未補cold costs的1800s／45of45只屬中途結果，最終以表中44of45為準。

既有一小時混合fixture修正為 `api_wait` 期間保留單仍可成交及接收行情，沒有調低原90%雙邊門檻：3600.105s、42 maker／8 taker，六個10min窗口皆報價，quote uptime3376.336s、雙邊3247.132s（90.20%），API退出0，REST351306／TX316，REST／WS／TX峰10000／118／12，funding −0.00038400912僅一次且final0/0與精確帳務通過。無可選延後的負例仍會在第三次API退出安全早停；安全收尾不冒充完整運行。模擬仍有風控IOC與錯過機會，不能推算真實量能或fee-cover。

本批保留進場時已有的未提交修改，沒有啟動 live、查詢交易帳戶、調整 quota／reserve／策略／風險／local live config、操作 Grid／VPS 或 commit／push。沒有shared production變更，不重跑無關Grid suite；沒有用flat network dry充作nonflat成本驗證。實盤持續一小時與 volume／fee cover 仍未通過，歷史失敗與成本不回填成功。

## 2026-09-10 Pro review：可選報價延後與完整固定負載驗收

沿用 `13174b1`，本批只修正常 API admission 與可選 create/reprice 的分流，沒有調整策略、spread、size、max hold、stop loss、配額、退出預留或 IOC 邊界。前述實盤故障與安全早停仍是歷史失敗證據。本批沒有連接交易帳戶、啟動 live 或操作 VPS。

`VolumeExecutionPort` 只在兩處產生 `DEFERRED`：建立缺少側之前，以及仍安全的既有報價撤換之前。延後需完整 fresh authenticated account／exact orders、健康 execution、原有效期內、仍 passive 且符合最新 governor capacity／reduce-only；部分成交剩餘量低於補單目標或新單 minimum 不會自行變成必須撤單。真正到期、風險／費率失效、未知訂單、stale account/book、必要 read/cancel 拒絕仍走原清理／退出，沒有全域 catch-and-continue。

可選撤換先按實際 selected sides 預檢：每側取消 REST412／TX1、撤後 coherent audit REST1200／WS5、建立 REST1400／WS8／TX2，加原10s quote deadline內會到期的 terms。所有原階段 admission 與撤後 fresh risk authorization 仍執行。延後保留真實 order ID／age／remaining，`actual_plan` 僅包含實際保留單；第二側建單延後也保留第一側已成功動作計數，不宣稱雙邊目標完成。

非 flat/empty 延後需另能支付下一監控輪 REST1200／WS5／TX0（兩側 terminal slot200、cash300、trades600、terminal fill100），以及5s內到期的 fee900／settlement300，外加不變的 scheduled exit reserve。額度檢查不是持有 reservation，arrival race／forced refresh仍經原 gates，失敗即退出。完整已查核 flat/empty 才可無新增 REST 等待；恢復仍須重新同步、查帳與風險授權。`api_wait` 不重置持倉年齡、損失、quote age或session deadline，stop可喚醒，睡眠裁切到原 quote expiry／hold／session deadline。

Sidecar 新增 `optional_waits`，與真正啟動退出的 `deferrals` 分開；只有後者套用原600s內第三次API退出後停止重入規則。最近64個 admission denials 保存 `operation`、`blocking_bucket`、精確字串 `blocking_offset_seconds`、`projected_usage`、`limit`；真正 backpressure exits 也保留相同欄位。Console 顯示 `api_wait`，其中正常監控步驟合併為60s heartbeat，完整 JSONL不抽樣。

**固定600s驗收已通過，而非以安全早停充作成功。** 原15個固定機會、每15s單側BBO移動、每波集中部分成交、原quota/reserve/risk完全相同；missed機會不延後重送。既有委託在 `api_wait` 仍可按原時點成交，並未以狀態名稱假設交易所暫停。新的獨立斷言要求完整600s、至少14/15，及final authenticated0/0、exactcash差額0。實測 **600.108s、15/15、REST76200、TX89、IOC5、可選延後23次、真正API退出1次**；不以不同時長的REST總量宣稱降幅。仍有正常風控／必要API退出與taker成本，離線成交不是venue fill或fee-cover證據。

原安全案例另保留，僅在測試中禁用可選延後 callbacks：selected sides **531.546s／14/15**、whole pair **264.025s／5/15**，兩者明確斷言第三次API退出早停及exact0/0/cash；不能滿足新完整窗口測試。新增 execution／session 反例檢查必要取消、未知／stale證據、預算不足、延後時新成交觸及停損，以及stop／原期限，成功收尾場次皆精確對帳。

最終 **530 V2 tests PASS，62.907s**（repository `.venv`／`unittest discover -s tests -p "test_mm_v2_*.py"`）。包含原60min離線fixture：3600.105s、maker42／taker8、REST375400、TX446、雙邊3338.561s、API退出1，funding僅一次且精確對帳。另已測flat/empty建單拒絕等候時stop／原deadline，以及延後後stale book的明確失敗＋已知單清理。`git diff --check`通過；沒有shared/Grid production變更，未重跑無關Grid suite。未commit/push，未進行新的network/live驗證。


**2026-09-10 20:46 場次204643：完整追查API觸發、撤換放大與IOC語義（本地）：** `logs/mm_v2_economics_20260910_204643_627.jsonl` 原定3600s，ledger385.6028150s／wall約448s後code1；12 maker fills／276.994580 USDG、2 taker fills／76.944880 USDG，gross−0.041240、maker fee0.03323934960／taker fee0.02693070800、funding0、realized net−0.10141005760。Final authenticated仍long0.00020／orders0，uPnL−0.010580；report的同額equity difference是尚未實現PnL，不是現金結算差額。前輪realized-gross修正已處理本輪多筆部分成交與兩次實際IOC，沒有再出現循環小數帳務錯誤；本輪all-in／fee cover仍不可用。

API sidecar為同stem `.jsonl.budget.json`：3次local deferral均在reconciling_quotes，REST used8106／7800／11000、next1400；1次account activity race。REST peak12100、WS111、TX9皆低於本地總上限，但admission還保留未來40s的IOC／final proof，近期burst的可用normal額度較低，不能直接拿24k−當前用量當可花額度。Startup／normal／exit REST2700／44006／7000；75次account、60次history、20次trades。50個confirmed quote results中36個no-op、5個單create、4個雙create、3個雙撤雙建、2個單撤雙建；正常no-op並未普遍收create admission。整對revision有額外讀寫，但不是所有成本的唯一原因。保持現有quota、reserve、source checks與風險參數，不以提前停止縮短量能分母。

Exit-1、exit-2確有IOC並新查核0/0；exit-3本已flat，attempts0。Exit-4的SELL原固定limit76812.5，prepare後fresh bid76810.6／ask76831.8；原bridge因bid低於limit而在送出前HALTED，submitted0，封死剩餘退出機會。[Lighter官方交易規格](https://apidocs.lighter.xyz/docs/trading)明示不能以等於或更好限價成交時取消委託；這是IOC的正常零成交結果，不能等同未知execution。已刪除多餘marketable前提，仍送原reduce-only限價，核對exact terminal與fresh residual；最多3次／原deadline／原限價不變。行情始終不回界內仍會ATTEMPTS_EXHAUSTED並明示殘倉，沒有保證成交或暗中追價。

同時修正一側revision強迫整對撤換：V2 manager新增選擇side的精確撤單入口，預設全撤保留；bridge只撤需更新一側，保留另一側ID／價格／數量／排隊時間。每次撤單後仍新查account／risk，若保留側因成交或風險變化失去授權，必須先撤除再新增；fee變更、兩側到期／cross仍可全撤。Dry採相同side語義，不虛構fills。這項修正針對可證明的讀寫放大，不以單場資料宣稱已足夠解決全部API壓力。

另修正兩項正常讀取成本：Unified非零持倉在原8s內、same generation／exact orders／trade count及fresh WS cash／position core完全一致時可沿用原現金與估值證據，不重蓋cash_at；Classic非零持倉、退出及final仍新REST。單獨uPnL變動不必觸發REST再讀；cash哪怕1e-11或position core改變仍重新查核，fresh BBO已穿stop時不能被舊正uPnL遮蔽。Normal audit起始REST1000拆為當段cash300，trades600／terminal100等各自既有gate保留；這些是預檢而非持有reservation，不把後段尚未需要的query先綁進起始預檢。

21:06:28 +08獨立authenticated唯讀確認position/orders **0/0**、cash297.856122541522 USDG、已disconnect，證據同stem `.postflight.json`。這是原程序結束後的新狀態，本次agent未送任何委託，不能回填成exit-4成功或原場完整all-in。不啟動新live，Grid／VPS不動。

正常撤單／建單admission也已分離：每個實際撤除side為REST412／WS0／TX1（四次terminal history400＋nonce初始／invalid-nonce刷新最多12），兩側824／0／2，零managed空plan不收mutation gate；建單仍1400／8／2。真SDK SignerClient與nonce manager、MM terminal-only取消流程測得正常無terminal四讀406、invalid-nonce路徑12，均單sendTx，不重送mutation或讀active-order WS；既有exit reserve未改。

**代表性集中成交負載沒有被調整到通過：** 固定600s、15個機會；每波35s內三筆同側加倉／反側減倉、每15s改變單側外部價格、原quota與風險、部分成交及完整現金對帳。所有missed／未執行機會保留。初始selected-only仍294.04s／8of15；加Unified非零現金reuse後延長到531.546s／14of15。Audit與cancel admission精確化在此負載沒有額外改善，不能宣稱每項修正都提高指標。最終同版本整對撤單對照264.025s／5of15／REST40400／TX65／IOC3；selected版本531.546s／14of15／REST73800／TX89／IOC5，均3次API保護後提前停止、fresh0/0與exactcash。總請求因運行更久而增加，不能以不同執行時間比較總成本下降；TX每個完成機會13→約6.36只是診斷。**14/15與532/600仍未達完整窗口，不稱穩定或fee-cover成功。** 測試通過只證明此壓力下如實停場／清倉與會計守恆；本地已能重現殘餘瓶頸，不要求再用一輪live找相同問題。

**本批最終驗證：507項V2 PASS（64.236s）**，含真SDK cancellation上界、雙向IOC界外零成交／恢復／exhausted、指定side terminal與cancel-fill race、Unified cache微小cash差異／entry及counter改變／fresh price stop。既有3600s平滑fixture仍完整、42 maker／8 taker、fresh0/0與exactcash；REST389300→376400、TX462→450，但API deferrals0→1、雙邊3359.118→3333.499s（約92.60%），不是所有指標均改善。此較平滑案例不能覆蓋600s集中負載的早停，兩者並列保留。沒有再用flat dry冒充集中成交證據，沒有啟動新實盤或Git提交。

**2026-09-10 20:31 場次：部分減倉的結算來源錯誤使帳務與退出一起失敗（本地修正）：** 啟動畫面及window sidecar標7200s，但實際載入的YAML仍3600s（檔案最後修改9/6 20:19:31）；不得把顯示文字當實際runner deadline。`logs/mm_v2_economics_20260910_203100_741.jsonl` 僅ledger24.7927920s／約86s wall後code1，3筆maker成交合計77.587860 USDG，maker fee0.00931054320；沒有runner IOC，bounded exit attempts0，final帳戶未確認，原場仍failed／economics_evaluated=false。API deferrals與account read deferrals皆0，REST峰11206／WS68／TX7；490筆book最大source age78.7375ms。本輪不是API背壓或延遲造成。

根因：SELL0.00040@77590.9、SELL0.00020@77602.1後，BUY0.00040@77577.7部分減倉。交易所該筆實際realized gross為 **0.006774**，V2卻忽略adapter已有的`realized_pnl`，自行按平均成本算成0.006773333…；此循環小數在精確cash bridge觸發`Decimal.Inexact`。正常quote audit、撤單後exit audit及final audit共用計算，且原`allow_unreconciled_cash`只跳過比較、未跳過先行計算，導致撤單後仍不能送有界IOC。不是未知委託或mutation未確認。

**使用者已確認後續BUY0.00020@77513.5是程序失敗後自行手動平倉。** 20:33:46／20:34:12 +08 authenticated唯讀皆position/orders **0/0**、cash297.940007024482 USDG，已disconnect；證據同stem `.postflight.json`／`.cash_evidence.json`。手動fill的gross0.016226、taker fee0.00542594500；獨立重建四筆gross0.023000−fees0.01473648820＝net **+0.00826351180 USDG**，精確等於相對起始cash297.931743512682的差額。這是含使用者手動退出的帳戶結果，**不是runner清倉成功或120分鐘fee-cover通過**；不回填原失敗journal。僅前三筆的正確realized net為−0.00253654320，當時仍short0.00020，不能當all-in。

修正：V2 live fill強制讀取有限Decimal的交易所realized gross並納入immutable fill identity與原JSONL；ledger以該筆結算值累計cash，據此分配部分減倉成本，不再以模型gross覆蓋平倉結算。缺失／非有限／同ID變更／同session混用來源拒絕；舊JSONL全程無欄位仍可按舊模型重播。無法精確拆分的結算rounding標記decomposition incomplete，不虛構spread／drift。正常及final仍要求精確cash相等，不加epsilon；exit-only既有例外直接跳過cash算式，其fresh position、fill完整性、ownership與terminal proof保留。未改shared adapter、Grid、VPS、quote、風險額度或API配置；未自行啟動新live或送出平倉單。

本輪新增真實三筆到正常snapshot／exit proof／最終cash的回歸、多空鏡像、反向建倉、現金差額與退出算式隔離、成交資料完整性及新舊JSONL重播。**完整V2 492項PASS（55.624s）**；既有完整3600s VolumeSession離線壓力測例仍保持REST389300、API deferrals0、42 maker／8 taker，未以早停換取通過。PowerShell啟動畫面與window sidecar已統一由既有config loader取得實際duration；本地YAML仍3600s，未延長交易期限，已驗證設定讀取與腳本語法。修後完整實盤與經濟結果仍待取得。

**2026-09-10，依524d99a分析降低讀取成本與重複清倉，並精簡console（本地）：** 本批不改quote、size、inventory／session loss、hold或IOC界線，不切帳戶方案。20:12:50 +08 authenticated唯讀確認RH `api.rh.lighter.xyz`／chain466324、premium、maker1.2／taker3.5bps，position/orders **0/0**，已disconnect；證據 `logs/mm_v2_read_only_fee_limits_20260910.json`。AccountLimits沒有REST／WS／TX限額欄位，RH專屬文件本次未能取得，其他network的Plus／主網費率不可直接套用；保留本地24000／200／40 guard，不能稱為本次已認證的交易所精確配額。現在0/0不回填9/7失敗輪的退出成本或all-in，亦不是本批送單平倉。

正常stream audit將fee/funding與settlement terms和快速帳戶證據分開：terms最多30s、28s開始刷新（留2s驗證餘裕），保留原request-start；cash／position／orders仍10s，cash reuse仍只在flat及原8s內。到期要讀terms時先取新cash，避免慢metadata query把舊cash帶過有效期。Funding fingerprint改變、cash bridge差額立即補查；新fill高於cached fee只觸發一次既有bounded refresh，fresh當前折扣不能否定較早fill的真實費率。Exit／final仍8s terms與fresh cash，IOC／最後對帳和完整原API reserve不變；stream替換及真正讀取錯誤使快取失效。新增optional `terms_observed_monotonic`，舊JSONL仍可重播。Shared adapter僅MM exact路徑附safe tier，Grid default不改。

10分鐘（含600s邊界）內第3次本地API背壓，先完成原bounded exit，再以 `api_backpressure_repeated` 停止重入，finally仍需獨立authenticated0/0與精確帳務；收尾失敗仍failed，AccountReadRace不混入API次數。既有budget sidecar保存本地限額、起始tier／實查費率、按startup／normal／exit計量的實際REST weight，以及最多64筆背壓phase／exit ID／時間／用量，沒有新增日誌層。提前停止保留原預定窗口，不能算完整長測成功。

以下沿用同一3600s Unified離線fixture、42個排程maker事件、funding／partial IOC／arrival race／改價壓力；不以提前停止節省成本：

| 指標 | 524d99a | 本批 |
|---|---:|---:|
| API背壓退出 | 24 | **0** |
| 全程REST weight | 515600 | **389300（−24.5%）** |
| Exit REST weight | 28800 | **21100** |
| 有單／雙邊秒 | 2666.051／2660.898 | **3363.925／3359.118** |
| Maker／taker fills | 42／10 | **42／8** |
| sendTx（含撤單） | 404 | **462** |

本批normal REST366100／60min＝6101.67weight/min，startup2100、exit21100；雙邊93.31%，全部6個10min窗有報價，funding−0.00038400912只記一次，原deadline與final0/0、cash精確對帳保留。TX因更多運行而增加，不宣稱所有請求或實盤費用都下降；fixture按排程成交，不證明真實成交機會／fee cover。回歸要求完整3600s、REST≤400000、背壓≤1、雙邊≥90%，高壓第三次停止另有專用測試，不用停場壓低正常成本數字。

Console `--progress` 將短暫quote步驟合併為 `quoting`，狀態轉換、fill、退出／exit ID、錯誤即時顯示，其餘每60s摘要成交额／費用／既有帳戶觀測與年齡；不新增API，不重蓋證據時間，console關閉不妨礙cleanup。原完整JSONL不取樣、不刪事件，console開關的journal bytes一致；既有run logs不清除。

**驗證：** 最終V2 **483項PASS（52.739s）**。Shared tier欄位後已跑全套765項：非V2仍為既有Grid 8 failures＋4 errors，方法名與baseline差異0；三個V2失敗揭露慢terms讀取的舊cash過期及舊90s fixture未留下新TTL後的重入窗口，已修正並由最終V2整套覆蓋。成本／quote表以最後cash保護版本為準，先前中間版383600不是交付數字。5分鐘strict dry `logs/mm_v2_terms_dry_20260910.jsonl` 完成、code0、fresh authenticated final0/0，無委託、economics_evaluated=false，程序已退出。3405筆strict book、最大age288.635ms、零超界；新dry和9/7歷史失敗JSONL均由現有analyzer重播，保持economics false。該進程啟動後補上的cash保護與heartbeat取新快照修正由最終離線套件覆蓋，不冒稱dry曾熱載入新碼。修後live／經濟驗證仍未進行。依本輪順序，下一個實盤比較先固定原報價與風險參數，驗normal REST/min與API原因IOC；取得改善證據後才評估有額度的一次短maker減倉，再比較edge，不同時堆入多項策略改動。VPS與commit/push未執行。

**2026-09-07 01:02，分步admission後的新實盤仍因退出缺陷失敗，殘倉未平：** 使用者場次 `logs/mm_v2_economics_20260907_000201_902.jsonl` 原定3600s，ledger1369.2596237s（22分49秒）／wall1432.1511296s、exit code1。現有analyzer獨立重播確認23個唯一maker fill IDs／21張maker委託，maker買332.116837／賣270.784500、合計 **602.901337 USDG**；taker143.326420／5fills／4張委託。Gross−0.007440、maker fee0.07234816044、taker fee0.05016424700、funding0、realized net **−0.12995240744 USDG**。仍有倉位，所以all-in net／fee-cover不可判定；final report的`equity_reconciliation_difference=0.002108`恰為當次unrealized PnL，**不是未歸因現金差額**。

**退出根因與現況：** 最後一筆maker BUY僅部分成交0.00017 BTC，exit-11撤單後在`exit_market`的quantity/minimum guard被拒絕，attempts0、未送IOC；不是`risk_capacity_exhausted`正常停場。該場194個正常execution results皆確認雙邊，沒有BUY-only循環。01:02:36 +08:00 fresh authenticated唯讀仍為 **BTC long0.00017／open orders0、cash297.938824540532 USDG**，disconnect完成；sanitized證據為 `logs/mm_v2_economics_20260907_000201_902.postflight.json`。這是稍後帳戶現況，不回填原場損益，也沒有實際平倉。

[Lighter官方交易規格](https://apidocs.lighter.xyz/docs/trading)明示base／quote minimum只適用maker orders。V2原先將maker minimum套到reducing IOC，會把合法step上的部分成交殘量擋在本地。已修正orchestrator與V2 order manager的IOC檢查；正常POST_ONLY仍遵守原minimum，IOC仍須實際正殘量、精確size step、reduce-only、固定價格界線及原次數／期限，不能增量湊minimum。**完整V2套件469項PASS（50.396s）**；完整runner使用BTC min base `.00020`、step `.00001`、min quote `10`，覆蓋多／空部分maker成交 `.00017`、IOC再次partial留下 `.00001` 後精確退出與現金對帳。Bounded exit／OM亦覆蓋非整lot、無效量、非reduce-only拒絕及固定limit／deadline，diff-check通過；未改shared／Grid或live config。這是本地回歸，尚無修後實盤退出證據。

**01:11:08 +08:00再次authenticated唯讀查核：** 仍為BTC long `.00017`／orders0、cash `297.938824540532`，當時uPnL `−0.003706`；disconnect完成。另存同stem `.postflight_final.json`，保留01:02證據；本批未送交易，現有殘倉尚未平掉，不能將離線final0/0當作真實帳戶狀態。

為免前輪最後28分鐘空轉扭曲效果，下表都取第一筆帳戶快照起的**前1369.2596秒**：

| 指標 | 09-06 21:48輪同時間段 | 09-07 00:02輪 |
|---|---:|---:|
| Maker額USDG／fills／不同委託 | 382.882640／14／12 | **602.901337／23／21** |
| 有單秒／雙邊秒 | 915.336／893.255 | **964.723／933.375** |
| 退出／需IOC退出 | 12／10 | **11／4** |
| Taker fee USDG | 0.11168629675 | **0.05016424700** |

本輪API deferrals10、account-read deferrals1；11次退出中最後一次blocked。Sidecar沒有逐次退出理由，不能指定哪次由account race觸發。較多maker、較少taker是觀察結果，市場與終止狀態不同，不能視為因果或fee-cover通過；API退出只小幅減少，quota問題尚未解決。前0–10／10–20／20–22:49分的maker額為222.957480／286.784320／93.159537，雙邊秒425.316／412.832／95.227；其後沒有運行，不算成功窗口。全期有單964.723s（70.46%）、雙邊933.375s（68.17%）；REST／WS／TX峰值13500／92／8，10997筆strict行情、最大age442.579ms、零source例外，沒有證據指向行情延遲。本批文件與本地修復不涉及Grid、VPS、Git或真實委託操作。

**2026-09-06 23:58，API預算分步檢查優化完成（本地）：** 本批針對214814實盤的18次API背壓退出，而非擴大risk或再放寬行情。該場REST weight518006，fees/funding/settlement合計278100（53.69%）、cash180600（34.86%）；其中exit-6前64.845s完全無fill，只有24.927s掛單、5個normal cycles仍被迫撤單退出。Sidecar沒有逐request時戳，不能唯一指認每次拒絕的bucket。先前`audit2200`不論metadata快取是否有效皆用全分支上界檢查，會在本次實際不需要昂貴讀取時提早驅離報價。

**實作：** 起始stream audit保守檢查1000，實際需要fees/settlement/trades/terminal history時，再在query之前各自admit900/300/600/100。每一步都保留完整原exit horizon；`require_normal`並不持有reservation，因此後置trades/history不能依賴最初1000或只檢查metadata就跳過。Settlement經admission後保存request-start，await成功才更新(value,at)，拒絕不把舊值標為fresh；fees與cash既有有效期限、同generation、exact ledger/funding/terminal證據皆保持。Exit新kinds沿原預留，額外retry/forced-funding限制不變。只修改V2兩個production模組，沒有新配置／排程框架，也沒有採用未驗證的長TTL或nonflat cash cache放寬。

以下是**同一個既有3600s虛擬Unified fixture、相同42個排程maker事件、partial IOC／funding／arrival race／間歇改價壓力**的前後比較；不是市場撮合重播或新增實盤。正式實作在兩次完整套件中產生相同結果：

| 指標 | 修改前 | 修改後 |
|---|---:|---:|
| API deferrals／背壓收尾 | 41 | **24** |
| 至少一側掛單秒／比率 | 2328.170／64.7% | **2666.051／74.1%** |
| 雙邊掛單秒 | 未保存本項基線 | **2660.898** |
| Maker fills／taker fills | 42／10 | **42／10** |
| sendTx次數（含撤單） | 440 | **404** |
| 退出REST weight | 38900 | **28800** |
| 全程REST weight | 480000 | **515600** |

背壓收尾減少41.5%，掛單增加337.881s（約5min38s／14.5%），退出REST減少26.0%；因可運行時間增加，總REST反而增加7.4%，不能說實際API總消耗下降。正式peaks REST12500／WS108／TX19皆在原上限內；6個10min窗口皆有報價，funding−0.00038400912只記一次、same deadline3600.099s、final0/0且現金精確對帳。原600s calm/moving-inventory情境現在零normal read拒絕，真正hold-time退出後仍等待API re-entry headroom；不能把正常風控等待也刪掉。

**驗證與邊界：** 465項V2都有通過證據：最後完整run464PASS，唯一舊assert將「零read deferral」誤等同「無risk-exit cooldown」，已修回保留真實hold退出等待並定向PASS（2.652s），production沒有再變動。Stop/deadline-in-cooldown測試改以明示一次API burst觸發，保留原期限／不重入／final0/0驗收，不再依賴低效率normal reads必然耗盡預算。四個新query邊界用真正ApiBudget拒絕，query未發送、metadata不重打時間；成交後trades/history被拒仍完成既有cleanup、同ledger/loss/deadline重入，fill不重複。長fixture另加入至少70%雙邊時間、deferrals≤30、taker fills≤原10的運作回歸線，避免只有等滿一小時也算優化；這些是固定離線工作量下的工程門檻，並非盈利驗收。

**尚未證明fee cover：** fixture會等有符合條件的委託才執行排程fill，不能證明真實市場成交機會。Taker fills仍10，不能從較少API退出直接宣稱手續費或實盤淨損改善。最新實盤仍為前兩輪maker748.968860USDG、已對帳淨損0.40383160995；本批沒有新增live/帳戶查核或委託操作，23:25的0/0只是上批最新authenticated觀測。風險／報價／資料時效／退出預留與ignored live config不變，Grid/shared production、VPS與commit/push未動；diff-check與review完成。

**2026-09-06 23:25，新增兩輪實盤已核對；成交增加，但完整轮後半段空轉，另一輪因 account race 提早收尾：** 使用者場次 `214814_774` 與 `230100_937`（完整檔名前綴 `logs/mm_v2_economics_20260906_`）均已用現有 analyzer 獨立離線重播，fills 無重複 ID，final authenticated position/orders 為0/0、exact cash bridge差0。第二輪雖然 `session_report.complete=true/failed=false`，這只描述ledger；其FailureDiagnostic與window code1證明runtime失敗，analyzer沒有將它當完整經濟候選。23:25:02 +08:00 fresh authenticated唯讀再次確認 **0倉／0掛單、cash298.068939505372 USDG**，disconnect完成，未見MM runner；sanitized `.postflight.json`附在第二輪stem。

| 指標 | 21:48 完整輪 | 23:01 不完整輪 |
|---|---:|---:|
| 原定／ledger秒 | 3600／3602.3099147 | 3600／282.7054843 |
| 含啟動全程序秒／exit code | 3665.5682161／0 | 345.6055001／1 |
| Maker買／賣額USDG | 334.890780／255.181920 | 63.517960／95.378200 |
| Maker總額／fills／不同order IDs | 590.072700／22／19 | 158.896160／6／5 |
| Taker額／fills | 526.274525／21 | 95.412180／4 |
| Gross／funding USDG | −0.091985／0 | −0.004380／0 |
| Maker fee／taker fee USDG | 0.07080872400／0.18419608375 | 0.01906753920／0.03339426300 |
| 已精確對帳net USDG | **−0.34698980775** | **−0.05684180220** |
| 有單秒／雙邊秒 | 1204.981／1156.884 | 151.569／144.546 |
| API deferrals／exits／需IOC退出 | 18／19／16 | 3／4／3 |

完整輪全wall maker速率 **579.518 USDG/h**；兩輪實際maker共748.968860、已知淨損共 **0.40383160995 USDG**。不完整輪仍保留原3600s分母，兩輪合計固定窗口速率371.105/h；不得年化短暫活躍片段充當達標。只有24個不同maker委託，且一輪未滿時長，1000/h、50委託、兩完整窗口及fee cover皆未通過。缺一完整窗口時analyzer aggregate all-in維持null，已對帳成本不會丟棄。

**先前capacity修正的實際效果：** 相較20:23輪，正常submitted/cancelled由226/150降為72/25、API deferrals54→18，BUY-only撤建循環已消失；雙邊掛單率24.48%→32.12%，有掛單期間大多兩側都有。Maker額414.11→590.07，淨成本卻3.9526→5.8805bps。完整輪taker額為maker的89.19%、taker fee占全部fee72.23%；forced_flatten_loss0.28182108375占net loss81.22%，已含taker fee，不重複相加。18次API背壓及19次退出中16次需IOC，頻繁收尾仍是成本主因，不能把恢復運作當作經濟成功。

| 策略分鐘 | Maker額USDG | 有單秒／雙邊秒 | Net USDG |
|---|---:|---:|---:|
| 0–10 | 191.453600 | 425.474／414.332 | −0.07916613550 |
| 10–20 | 127.638200 | 362.947／354.481 | −0.07130717125 |
| 20–30 | 207.249500 | 363.588／342.921 | −0.14353080700 |
| 30–40 | 63.731400 | 52.972／45.151 | −0.05298569400 |
| 40–50 | 0 | 0／0 | 0 |
| 50–60 | 0 | 0／0 | 0 |

**本輪確認的兩個缺口：** 完整輪最後maker在t1908.446s，exit-18於t1912.605s平倉，t1952s起flat/no-orders但持續REDUCE_ONLY空計畫直到deadline。剩餘risk headroom為0.15301019225，而BTC最小量.00020在當時fee/200ticks下需約0.163397USDG預留；甚至零價格／零fee仍有.15 stop＋.004 slip=.154，不能靠等待行情恢復。這是沒有可執行容量時的停場條件缺漏，非先前BUY撤建問題復發，也不以提高loss limit解決。

不完整輪原錯在authorizing_quotes→`lighter_runtime:423`，兩次audit仍遇WS／REST bracket不一致；manager當時healthy、known雙邊、unknown/uncertain皆false。隨後退出讀到同一SELL委託的.00031＋.00009 BTC partial fills，source相隔98ms，支持成交與不同讀路徑更新競態；既有日誌沒有mismatch子項，不能唯一斷言orders/counter/financial tuple哪欄失配。完整／不完整輪strict book最大age582.284／54.436ms、零source例外，沒有證據指向時鐘或行情延遲為本輪主因。

**本地修復與驗證：** flat、無掛單、兩側皆沒有符合minimum的capacity時，沿既有30s bounded exit取得fresh proof才SESSION_COMPLETE，摘要明示`stop_reason=risk_capacity_exhausted`；有倉保持reduce-only，有舊單占用risk時先撤後重新評估。正常account activity race保留兩次／10s上限，僅健康stream且known執行狀態才能沿bounded cleanup→fresh0/0→API headroom cooldown→strict reauthorization恢復；cash gap、資料／身份衝突、未知wire、cleanup不完整仍失敗。正常讀取延後另計`account_read_deferrals`，不混為API拒絕或財務成功。**461 V2 tests PASS（50.152s）**、獨立檢視與diff-check完成。真實runner fixture讓同一known maker在REST cash後成交，第二讀WS仍舊，耗盡兩次bracket後在正常授權及第一側已送出的兩入口先清倉再strict reauth；保持同ledger/loss/deadline、fill去重、無未證明期間POST_ONLY。未知wire與persistent mismatch不重入，cash gap負例保持。另驗證原3600s但risk reserve不夠的flat場次在10s內完成fresh退出、零create/IOC且回傳明示原因；真實BTC量／損失的governor例及舊單可撤後恢復負例也通過。既有3600.100s虛擬跨funding/API場次仍42maker/10taker、6個10min皆報價、quote2328.17s、final0/0，非新增實盤經濟證據。

此批未改size/loss/API/source限制、報價參數或ignored live config，未啟動live或操作委託；Grid/shared production、VPS與commit/push未動。後續實盤仍須重新取得修後證據，現有兩輪不能證明high-volume/fee-cover；下一個經濟問題是API背壓引發的頻繁taker退出成本，不再把BUY修正、讀取恢復或程序時間到當作策略成功。

**2026-09-06 21:45，完整一小時實盤已完成收尾，但經濟與持續雙邊報價未通過：** 使用者場次 `logs/mm_v2_economics_20260906_202303_240.jsonl` planned3600s、ledger3601.798s、完整wall3664.559s、code0、completed/economics_evaluated=true、exact final0/0、cash bridge差0。獨立analyzer重播通過；21:37:58及21:47:14 authenticated唯讀皆position/orders0/0、cash298.472771115322、disconnect完成且未見MM runner。`economics_evaluated`只代表帳務可評估，非economic GO。

Maker turnover **414.112524 USDG**（15個fill IDs／13個order IDs，買286.185684／賣127.926840），含完整wall速率 **406.817 USDG/h**，低於預設1000/h驗收值。Taker turnover286.182458／10fills；gross−0.013826、maker fee0.04969350288、taker fee0.10016386030、funding0、all-in **−0.16368336318 USDG**、cost3.95263011bps。費用占淨損91.55%，其中taker占全部費用66.84%；forced_flatten_loss0.13126986030已含退出費用，不能與fee再相加。54次API deferrals/退出中9次需IOC；peaks REST12600／WS100／TX17，25738筆strict book age14.1011–877.7953ms、零source例外。沒有把cooldown當成有效報價或把taker額灌入maker volume。

| 策略分鐘 | Maker額USDG | Maker fills／訂單 | 掛單秒／雙邊秒 | 退出／需IOC | All-in net USDG |
|---|---:|---:|---:|---:|---:|
| 0–10 | 190.294764 | 7／6 | 416.52／406.00 | 5／4 | −0.07008744848 |
| 10–20 | 159.883440 | 6／5 | 418.35／409.60 | 4／3 | −0.05443552080 |
| 20–30 | 63.934320 | 2／2 | 159.59／65.98 | 11／2 | −0.03916039390 |
| 30–40 | 0 | 0／0 | 121.10／0 | 11／0 | 0 |
| 40–50 | 0 | 0／0 | 121.63／0 | 12／0 | 0 |
| 50–60 | 0 | 0／0 | 116.84／0 | 11／0 | 0 |

全期quote uptime1354.026s（37.59%）、two-sided881.578s（24.48%）；最後maker成交在t1293.817s，後約38min沒有成交。t約1345s起多次submitted_count2但snapshot只有BUY；後25min的282個quoting marks全BUY，202個有單區間平均僅2.084s、最長3.700s。這是telemetry觀測區間，非exchange精確壽命。後40min有46次較小plan撤單後又變大actual plan，`.00036/.00036 → .00040/.00040`直到最後，之後再因quota背壓收尾；不是沒有下單，也不是本場延遲檢查卡住。

**根因與本地修復：** governor原先以old＋desired、cap減old計「新增量」，policy與execution revision卻按每側「期望總量」比較。已實現費用縮小餘裕後，一張BUY存在便令next target變小→撤BUY→空倉容量回大→再建BUY；第二側因此無法留下，造成API消費／冷卻自我循環。已將governor統一為每側目標總量：曝險及maker fee採`max(old_remaining,target_total)`，保持one-slot/exact cancel後fresh授權的執行前提；保留old prices/gap、舊單本身不安全時拒絕、soft/hard/loss/step與IOC費用預留。未增size、loss、API limits或調整spread／200ticks候選。

**455 V2 tests PASS（47.725s）**、diff-check與獨立review完成。以本場realized net−0.16368336318、drawdown0.164885484780、maker/taker fee.00012/.00035、order.0004/hard.0008、stop.15/session.5及BTC lot建立真實port/governor/OM紅綠：舊碼只留BUY，修後一次完成兩側且同ID連續11個5s cycles零撤換，60s到期才exact cancel／fresh reauthorize更新。負例保留old risk、loss reserve、soft縮量、hard超限、未知terminal禁止替換；既有虛擬一小時跨funding/arrival/API恢復也通過（42maker／10taker、6窗口報價、final0/0）。這些證明本地契約修復，不能推算修後真實成交數或fee-cover。下一個有資訊價值的驗證是相同風險參數下觀察修後有效雙邊時間與全成本；本批未啟動live或操作真實委託，未動Grid/shared production、VPS或commit/push。

**2026-09-06 20:20，退出行情契約修復與較寬限價候選準備完成（本地）：** 使用者場次 `logs/mm_v2_economics_20260906_195101_523.jsonl` planned3600s、全程序388.218s／ledger窗口325.140s後failed。第1次API背壓退出成功（1 IOC平0.00040），第2次退出的SELL IOC `562950012515419` 於19:57:25、amount0.00037、limit79874.2、filled0、CANCELED；下一次flatten call在送單前的market guard被拒絕。`attempts=2`是兩次bridge呼叫，這次退出實際只送1 IOC。最終authenticated position/orders為 **long0.00037／0**，exact cash bridge通過，非上輪funding故障。1647筆strict book age14.2854–202.9166ms、無source failures；本場API deferrals2、peaks REST12800／WS107／TX11。

**已證實的缺陷與證據界線：** 同一guard混合metadata、prepare後的book時間與固定價格界線；舊diagnostic沒有相應scalar，還沿implicit exception context把已處理的normal API拒絕寫進flatten錯誤，不能由該trace把IOC失敗說成quota不足。離線完整runner重現合法同generation handoff帶入prepare前2ms的book，age僅約19ms仍被exit guard鎖停。現在每次exit account read清除舊opening/confirmation/audited handoff，取得prepare後的新opening與book，保留OM preparation generation及原source/nonce checks；紅／綠對照維持同limit97、同size0.1、同deadline，首IOC零成交後第二IOC可flat。真adapter已有直接IOC terminal history，因此這是可重現的consumer契約缺陷，**不能宣稱195101場必定走了該handoff分支**；該場也可能是BBO越出固定limit。API恢復收尾移出except handler，避免新故障繼承已處理拒絕；三項market guards分開並只記白名單book age、相對prepare時間、bid/ask/limit Decimal，沒有provider message或身份。

**退出候選只改本地長測設定的一欄：** `test_live_economics_60m.yaml` 的ioc_slippage_ticks由20改200；BTC tick0.1即整次退出相對初始BBO的價格範圍由2增至20 USD/BTC。既有order/soft/hard 0.00040/0.00040/0.00080、session loss0.50、inventory stop0.15、3次／30s與整輪約20USDG授權背景不變。最大倉位的整次價差cost cap由0.0016增至 **0.016 USDG**；此次0.00037殘量為0.0074，均不含退出前市場損失、fee、funding或多次退出累積成本。Governor已按同一ticks預留taker fee及slippage，retry只縮quantity、不重算／逐次擴張limit，所以3次partial不是將0.016再乘3。這是事先固定的下一場候選，沒有改写已失敗場次的20ticks設定與證據，也未啟動交易。

**452 V2 tests PASS（48.649s）**，包含新的完整runner時序紅／綠、cleanup診斷來源隔離、stale/untrusted／price超界不送單、白名單防洩漏，以及既有一小時跨funding/arrival/API恢復場次；後者仍42 maker／10 taker、41次deferrals、quote uptime2328.17s、6個10min均報價、API不超限及final0/0。另以原0.00037與首BBO79876.2建立明示的移價情境：首IOC零成交後假設bid變79872.6，20ticks在第二次送前拒絕，200ticks的兩張IOC皆固定limit79856.2、第二張flat。後者是價格界線的離線情境，**不是取回了當時真實BBO的replay**；設定parse後逐欄確認只有ticks不同，diff-check與獨立review通過。未改shared/Grid production，本輪不重跑上批已核對的Grid baseline。依[官方IOC定義](https://docs.lighter.xyz/perpetual-futures/orders-and-matching)，未立即成交部分會取消；200ticks仍不保證流動性、來源或網路異常時必定平倉，所有退出fee與讓價仍計入economics。

**20:20:09 +08:00 fresh authenticated仍long0.00037@79875.2／orders0**、cash298.615581246702、當次unrealized+0.017723，disconnect完成且未見MM runner。這是後續帳戶現況，不回填19:57已結束的run。場內3筆maker fills（2個maker order IDs）turnover61.515144、maker fee0.00738181728；1筆taker turnover31.956960、fee0.01118493600；gross−0.004360、已實現net−0.02292675328，非flat故最終all-in不可用。Agent未啟動live或送撤單／平倉，殘倉仍需操作者處理；修後長時live及high-volume/fee-cover沒有PASS證據，未動VPS或commit/push。

**2026-09-06 19:23，19:00 funding 結算與退出耦合缺陷已定位：** 使用者場次 `logs/mm_v2_economics_20260906_184748_935.jsonl` planned3600s、全程序738.858s、ledger窗口676.097s後failed。正常API背壓已在同場成功恢復4次；最後並非quota拒絕，而是 `authorizing_quotes` exact cash bridge不符。第5次退出撤除兩張known orders後，被同一現金對帳要求擋住，IOC attempts0、final account缺失。19:01至19:23的獨立authenticated唯讀均見 **BTC long0.00040、open orders0**，不能把前4次flat proof當作本場final0/0。

**確定的現金來源與時間單位缺陷：** 7筆maker turnover214.315996／fee0.02571791952、3筆taker turnover54.384582／fee0.01903460370、gross0.005686均與authenticated fills及realized PnL相符。19:00 funding ID67904的authenticated `positionFunding.change=-0.000384`只有6位、timestamp1788692400是秒；WS funding_histories仍為空。獨立同市場同round public `value=0.96002280` × authenticated long size0.00040，得支出 **0.0003840091200**。19:20:10以修後production bridge對真實歷史做唯讀重播，28筆舊funding baseline後只處理該新ID、timestamp轉1788692400000ms、重讀相同且不重計；baseline298.75160420916＋gross−fees−funding = **298.7121536768200**，與當時真實cash逐位一致。沒有把差額補成funding。`value`的每單位現金語義是由[官方公式](https://docs.lighter.xyz/trading/funding)與獨立round資料支持的工程推論，SDK未明示；正常/final仍須完整精確現金對帳。

**本批修復：** shared adapter只在MM opt-in處理秒→毫秒、authenticated funding ID與不可變欄位、同輪public rate/direction/amount驗證及原子快取；Grid/default行為保留。Cash先到但WS funding不變時，在原10s／最多兩次audit內強制刷新一次funding及trades。收尾只對cash bridge允許未歸因差額，仍要求fresh identity/fees/position、exact fills與orders/ownership/terminal proof，再沿原30s／最多3次reduce-only IOC退出；無法證明的新funding在退出暫略、不標已入帳、不造現金。正常報價與最終economic proof維持strict；缺口未解則整場failed/economics=false。CLI另列 `cleanup_authenticated/position/open_orders`，僅表示已完成退出的取證，後续新報價前即清除，不能冒充final account。Public round300納入normal audit2200及退出立即8806預留；未放寬quota、source checks、損失、size或5s cadence。

**本地驗證完成：** shared production變動後跑完整733 tests（59.355s）。Grid既有8 failures＋4 errors與歷史baseline逐方法比較差異0；本輪另有CLI fixture誤用None、API reserve增300後budget fixture仍要求零deferral／空倉也必cancel等4 failures＋1 error，已修正測試資料與驗收不變量並重驗通過，沒有降低reserve或把runtime失敗改成PASS。現有450個V2 cases均有通過證據；其餘shared回歸無新增失敗，獨立review與diff-check完成。新負例確認未知1e-11 cash gap不可正常報價，也不能偽造funding，但完整執行證據下可同期限減倉至0/0，最後仍economic false；position mismatch連退出也不得通過。

連續虛擬Unified **3600.100s**情境在中段≥1800s持有long時扣款−0.00038400912，WS funding全程空，cash race必須forced refresh；相同funding ID重複返回只記一次，public round亦只計一次300。結果 **42 maker／10 taker、41次背壓恢復、6個10min窗口皆報價、quote uptime2328.17s（約64.7%）**，final0/0且現金精確對帳，proxy peaks REST12300／WS99／TX18。另逐次驗證denial後30s內authenticated flat、當時有單才撤、任何恢復報價前必有新strict account authorization、等待中的stop與原deadline不重啟。這是持續控制流程與最低API成本proxy，非真實網路長測；較高reserve降低可報價時間，不能把等待／頻繁taker退出算作volume或fee-cover成功。

**19:23:17 +08:00實際帳戶仍有殘倉：** BTC long0.00040@79964.1、orders0、cash298.71215367682、當次unrealized−0.021160；唯讀disconnect完成，先前process check未見MM runner。本批未啟動live或送撤單／平倉；殘倉需操作者透過交易介面處理。現金減少0.03945053234尚不含這筆殘倉的未實現／未來退出成本，不能報本場最終all-in或fee-cover。VPS與Grid專用程式未動，未commit/push。

**2026-09-06 18:45，反覆短場失敗的控制流程與驗收標準修正完成（離線）：** 使用者明確要求停止live→錯誤→單點修補的循環。最新 `logs/mm_v2_economics_20260906_182813_427.jsonl` planned3600s、全程序148.374s、ledger窗口85.410s後code1；第二筆maker已令持倉回零、仍有一張known SELL，authorizing_quotes第二次audit被拒絕。診斷當下used REST10200/WS74/TX4，next1900/5/0。即使REST當下小於24000，scheduled退出前綴仍可能不容納下一讀；review未發現reserve重複記帳，沒有刪reserve或再次微調輪詢秒數。478筆strict book age13.985–175.6382ms，无source failure。自動撤剩餘單、0 IOC、exact final0/0；2筆maker成交額63.962600、gross0.002440、fee0.00767551200、all-in **−0.00523551200 USDG**。本場仍failed/economics_evaluated=false；兩筆交易不能證明volume/fee-cover成功。

**系統缺口與先前驗收不足：** `ApiBudgetUnavailable`是本地普通工作暫時無餘裕，卻被account wrapper改成LighterReadError、execution port改成BLOCKED，最外層一律結束場次；先前只對成功risk exit等待，沒有處理normal admission拒絕。舊高改價測試甚至把budget拒絕後completed=false當PASS；600s fixture只有一次maker及靜態book，預設Classic也未覆蓋實際Unified。先前430 tests證明部分安全收尾與局部恢復，不能作持續運行依據；18:27「持續運行修正完成」僅對當時那個離線情境成立，最新實盤已揭露其不足。

**本批修正：** account、normal quote及已確定撤單後的account proof保留typed local budget exception，normal loop專門處理：停止新風險→既有bounded exit→authenticated flat/empty→api_cooldown→fresh sync/account/risk後重新POST_ONLY。沒有用重啟程序重設ledger、損失或3600s deadline。只有budgetactive、非exit期、無unknown/unresolved wire且known slots可核對時才走恢復；exit期耗盡、真正資料/網路/429錯誤及cleanup failure仍failure。預期背壓計入`.budget.json`的`deferrals`，不寫FailureDiagnostic誤使整場永遠economics invalid；未恢復故障仍保存原診斷。API limits、退出預留/attempts、financial/source freshness不變；本批未再調節5s cadence、size、fee或quote policy。

**434 V2 tests PASS（47.093s）**，diff-check與兩條獨立review完成。五個參數化normal邊界涵蓋sync、第一audit、已接受fill後第二audit、已送一側後refresh、兩張已terminal cancel後account proof：每次30s內fresh0/0、等待期间不加風險、同ledger/deadline/loss重入，無FailureDiagnostic。未知wire與cleanup failure只退出一次、不等待重入；既有stop/deadline/partial IOC負例保留。舊高改價測試改為必須在同場恢復至原deadline，不能再將提早失敗算PASS。

一場連續**3600.099s虛擬Unified**跑完42筆scheduled maker、終態晚一讀、counter實際落後後追上、動態mark、間歇高改價、真實設定的60s quote age、每30s ping、至少一次3段IOC與反覆budget恢復。結果：**26次背壓恢復、42 maker fills、10 taker fills、6個10min窗口均重新報價、quote uptime2699.094s（約75.0%）**，所有maker排程執行且延後≤120s，原deadline不變、最後exact ledger/position/orders0/0，API peaks REST13200/WS108/TX20未超限。報價時間明示，不能以整場等待到3600s假稱持續運行。此為SDK重試/native startup之外的最低端點成本proxy，加上顯式control frames；仍不證明真實長網路窗口或fee cover，壓力情境的頻繁退出也有經濟成本，不把恢復能力當成獲利。

**18:39:39 +08:00 fresh authenticated position/orders0/0**、cash298.75160420916、disconnect完成且未見MM程序；同場`.postflight.json`保存sanitized證據。所有本批工作為V2本地程式/測試/文件與唯讀核對，未啟動實盤/送撤單、未改Grid/shared production、未動VPS/commit/push。修後實盤穩定性及high volume/fee-cover仍未取得證據。

**2026-09-06 18:27，API預算持續運行修正完成（離線）：** 使用者場次 `logs/mm_v2_economics_20260906_181102_288.jsonl` planned3600s、全程序290.535s、ledger窗口227.735s後code1。首錯authorizing_quotes→account audit→`ApiBudget.require_normal`，當時兩張known LIVE且無uncertainty/unknown；這次確定是普通讀取碰到退出預留守門。退出後peaks REST13800/WS133/TX8不是拒絕瞬間，不能斷言哪個bucket先耗盡；新增診斷保存拒絕當下used/next的六個數值。1061筆strict book、age14.851–149.5273ms，沒有source failure。自動撤單、1次IOC及exact final對帳成功：maker成交額31.960520、fee0.00383526240；taker31.965760、fee0.01118801600；gross−0.005240、all-in **−0.02026327840 USDG**。保持failed/economics_evaluated=false。

程式與加速重現確認三項成本/恢復缺口：nonflat cash reuse會因正常mark valuation改變觸發完整第二audit及forced trades；固定3s完整查核在成交/改價後餘裕不足；成功risk exit消耗預留後立刻re-entry又碰normal gate。修正為cached position非零直接fresh cash，省掉注定容易失敗的第一讀（exact cash、position、metadata/source freshness不放寬）；active API budget實盤cycle改5s，乾跑不變；僅在成功authenticated flat/empty退出後等待rolling容量恢復，以REST6000/WS32/TX4為重新進場餘裕目標而非完整cycle上界，所有read/mutation仍各自admit。等待每1s可被stop喚醒、到原session deadline即結束；恢復前重新同步/account/risk，ledger、損失、deadline及退出attempts不重置。三IOC退出預留、API限額與原硬拒絕保持不變。

**430 V2 tests PASS（17.681s）**，diff-check與獨立review PASS。新增600s虛擬場次沿用60s quote age、運行180s後成交、每cycle移動mark PnL及30s ping，舊流程在195s提早失敗；只修cache仍不足，5s節奏後揭露risk exit再入場缺口。最終完整600.099s、零admission拒絕、1 maker+1 IOC、0/0，且IOC後確實重新POST_ONLY報價；proxy peaks REST12700/WS95/TX8。另驗證空倉API等待中的stop與原deadline、持續高改價仍拒絕且使用預留收尾、3次partial IOC、nonflat mark變動不查多餘history/不retry及數值診斷。這是既有fake adapter最低成本模型加上control frames的離線證據，不包含所有真實SDK重試或其他IP消費者，不能當live長測或經濟成功。

**18:26:42 +08:00 fresh authenticated position/orders0/0**，cash298.75683972116，disconnect完成且無MM程序；同場`.postflight.json`保存sanitized證據。未啟動實盤或執行交易、未改Grid/shared production、未動VPS/Git。本地修正完成，修後live穩定性與volume/fee-cover仍未取得證據。

**2026-09-06 18:10，已知訂單終態延遲的正常恢復修正完成（離線）：** 使用者場次 `logs/mm_v2_economics_20260906_175913_210.jsonl` planned3600s、全程序72.578s、策略9.861s後code1。首錯authorizing_quotes，BUY known LIVE、SELL known UNCERTAIN_SUBMISSION，unknown=false；account已讀到成交但OM缺終態，原normal recovery以`not has_uncertain_state`排除了這個可恢復狀態。退出流程隨後成功取得proof、撤BUY、1次IOC平倉；final authenticated0/0且ledger difference0E-11。Maker SELL成交額31.948440、fee0.00383381280；taker退出成交額31.951480、fee0.01118301800；gross−0.003040、all-in **−0.01805683080 USDG**。這是自動退出成功、場次失敗，economics_evaluated=false。117筆book age14.7582–25.9038ms、無strict source failure，API peaks REST7406/WS51/TX4，未見延遲或quota拒絕。

正常`_authorize`及execution port初始sync兩個入口現在允許對known orders的延遲證據做一次有界重讀；前者重新取得完整account proof並重算風險，後者須恢復HEALTHY才進入fresh quote。集中`can_reconcile_known_orders`要求無local wire ambiguity latch、無unknown orders、無unresolved submit/cancel且slot ID均屬本manager；退出也沿用同一條件。仍需exact terminal proof，不清旗標強行恢復、不重送不明委託。重读在原deadline及normal API admission內，保留退出預留；第二次sync亦捕捉post-only rejection，必須遵守既有新book/cooldown。

**427 V2 tests PASS（13.114s）**，diff-check PASS，獨立唯讀review完成。先以完整runner重現舊碼提早失敗，再驗證terminal晚一讀時繼續至原deadline、1 maker+1 IOC、final0/0；永久缺proof不得新增/撤單/IOC。另覆蓋port內同步入口，以及adapter registry為空但local wire結果未知時仍拒絕。**18:09:47 +08:00獨立authenticated position/orders0/0**、cash298.77710299956，disconnect完成且未見MM程序；同場`.postflight.json`保存sanitized證據。本批僅改V2程式/測試及文件，未啟動實盤或執行交易、未動Grid/VPS/Git。修後長窗口穩定性、volume及fee-cover仍待實際證據。

**2026-09-06 17:56，第二場中斷後本地修復完成：** 使用者場次 `logs/mm_v2_economics_20260906_173545_494.jsonl` 於17:37:23退出code1；planned3600s、全程序97.910s、策略34.966s。首個diagnostic是authorizing_quotes→`_AccountReadRace`→fills/position不一致，當時兩張known LIVE、health healthy且無uncertainty；整場只在初始查過1次trades。後續authenticated查到BUY `844424871170831` maker成交0.00040@79911.2、fee0.00383573760，SELL `562950012469300` CANCELED。未變的total_trades_count讓normal及retry都跳過history，是已定位的成交發現缺陷。其後cancel/final account在舊Unified cash/summary共用守門失敗，IOC attempts0；缺少當時scalar，仍不能指認哪個值或特定rounding錯誤。283筆book age14.5286–79.7791ms均strict accepted，peaks REST9906/WS86/TX4，沒有source-age或quota拒絕證據。

修復將counter作提示：cash/position/entry變動或既有retry都會查100筆history；合法terminal缺fill也走同一次retry。已驗證但counter未反映的fill保留ahead，counter追上不重記；counter超前但缺history、下降、窗口不足仍拒絕。金融指紋排除order counts、mark PnL與cash reuse狀態，涵蓋classic cash roundtrip。OM sync後/account audit前成交則在同10s內消費一次已驗證orders handoff、同步並重新核對account/terminal，再重算風險；port只允許原known order減量或exact terminal移除，新增/加量/換價仍拒絕，retained quotes採fresh execution。

Unified改為明確的估值authority：fullprecision margin cash與fill/funding/fee仍逐位對帳，PnL在bridge兩側抵銷；保留cash/collateral、exclusive cross1x、metadata、finite且相等total/cross及flat summary。Nonflat取消沒有共同mark/precision官方契約的`trunc(cash+serialized PnL)==summary`要求，沒有epsilon；這是設計修正，不是假稱已證明rounding bug。獨立review確認governor仍取API loss與fresh touch loss較保守者。另新增whitelist Decimal診斷及cash/valuation錯誤分流，不含身份/token/raw payload。來源及限制見ARCHITECTURE。

**423 V2 tests PASS（13.043s）**、diff-check PASS。涵蓋counter全程落後的完整runner（OM sync後成交，正常繼續至deadline、1 maker+1 IOC、final0/0）、counter追上去重、terminal先到、持續缺history、classic roundtrip、nonflat獨立summary，以及多0.00000000001 USDG未知現金仍拒絕。獨立review抓到並修正cash reuse令指紋形狀改變的額外600REST；快取/mark/order-only零額外history、calm active-admission、三次partial IOC及高改價拒絕後cleanup皆包含於suite。歷史本場analyzer成功解析4個faults且economics_evaluated=false；ledger尚未納入已由独立API確認的maker fill，不能把程序內fill0當真實零成交。

**17:55:55 +08:00 fresh authenticated position/orders0/0**、cash298.79515983036、disconnect完成且無MM程序；17:39/17:41讀值亦0/0。這是外部已平倉觀察，不是runner退出成功或agent平倉。證據為同場ignored `.postflight.json`及`.read_contract.json`；累積成本不因新場次重置。舊場限定heartbeat `mm`已按0/0完成條件PAUSED。本批未啟動實盤或送撤單、未改Grid/shared production、未動VPS/Git；修後live穩定及volume/fee-cover仍未通過。

**2026-09-06 17:32，故障診斷與完整帳戶核對修復完成（離線）：** 使用者要求針對無法穩定運行的原因開始修復。已確認兩個工程缺口：既有arrival-race retry只包stream read，後面的fills/position與terminal history核對不在其中；execution port會先吞原始例外並回BLOCKED，上層只留下ValueError。修正為完整audit最多兩次、共用原10s與generation；僅新增「exact terminal history缺一讀」及「fills與position不一致」兩種可重讀分類，永久缺失仍拒絕。Metadata cache不在兩次間清除、第二讀禁cash reuse，已接受fill以ID去重，完整proof後才推進stream checkpoint；REST-only維持單次並受相同硬timeout。錯identity／duplicate history／無效數量／immutable proof衝突／Unified summary仍硬拒絕。重讀history100另納入預留：retry1000 REST／5 WS，immediate reserve8506，schedule prefix同步更新；不是放寬quota或新增迴圈。

同一JSONL新增typed `failure_diagnostic`，在quote/cancel/IOC介面把原始例外轉BLOCKED前捕捉stage、error type、V2 source module/line及已知slot/uncertainty狀態，並保留其後exit/final/disconnect錯誤。沒有raw exception text、locals、account config或SDK payload；sink甚至warning-as-error失敗都不阻止原cleanup。Analyzer解析新事件，保留final report後的disconnect故障，CLI失敗場次即使財務report完整也不標economic驗證完成。Runtime timeout regression實際在第一側已LIVE、第二側refresh前失敗，因此診斷正確保留一張BUY，沒有捏造雙側狀態。

**415 V2 tests PASS（13.405s）**，含4個full-audit arrival／永久缺失／deadline／generation／metadata cache及fill去重測試，另驗quote與cancel原始錯誤順序、診斷寫入失敗仍清理、秘密不序列化、analyzer與CLI失敗門檻。獨立離線review確認cache／checkpoint／預留一致。僅V2與其CLI/analyzer/tests/docs修改，Grid/shared production本批未改，未跑無關full suite、未啟動網路dry/live、未下撤單、未操作VPS或Git。上述機制缺陷已修復，但舊場沒有足夠原始錯誤證據，仍不能宣稱確證17:02場次唯一根因；連續live穩定與high-volume／fee-cover仍待實際完整窗口證據。

**2026-09-06 17:16，本場 live 監控與退出修補：** 使用者自行啟動 `logs/mm_v2_economics_20260906_170222_693.jsonl` 後要求監控。程序已於17:04:16以exit code1停止；planned3600s、全程序113.427s、策略50.658s，bounded exit為BLOCKED／attempts0／final_result null。17:15:47 +08:00 fresh authenticated唯讀核對仍有BTC short0.00040（entry79834.3）及BUY `844424871186215` remaining0.00040 @79809.3；SELL `562950012456136`已maker成交0.00040，fee0.00383204640 USDG，cash298.85489623641。當時unrealized−0.020080只是時點觀察，非最終損失。本場未完成、非final0/0，不能判定volume／fee cover成功。已通知使用者自行處理真實持倉與掛單；建立本場限定每5分鐘唯讀heartbeat（`mm`），有實質變動／查核失敗才通知，fresh0/0且程序退出後暫停。該監控不執行交易或損失上限。

離線重現已知訂單從active消失、第一次history尚無精確終態的路徑：OM正確標示uncertain，但原退出條件因此連一次唯讀恢復都不允許。此次僅改V2 `_exit`：無unknown ownership、adapter submission/cancellation registries皆空、所有slots都有本runtime已知exchange ID時，使用既有一次retry allowance做sync；real resolver在空registry時零I/O返回。精確active／terminal證據及後續健康門檻仍決定能否清理，不手動清旗標、不重送、不增加原deadline／IOC次數。最大500 REST／2 WS讀取仍包含在既有900／5預留。新增runner正負回歸驗證延遲終態補齊後cancel＋減倉、證據始終缺失則保持BLOCKED；原unknown submission測試加入真正unmatched adapter registry。**407 V2 tests PASS（12.811s）**，diff-check PASS，獨立離線review無實質問題；本批shared/Grid不變，未啟動實盤或操作交易委託。此機制已證實存在，但原日誌未保存首個例外／失敗時slot狀態，因此**尚不能確證本場根因**；行情source樣本無超時、sendTx僅2次，不能據此推定已修復所有退出問題。新碼未經live驗證；sanitized postflight保留於同場`.postflight.json`。

**2026-09-06 17:00，本機啟動可見性修正：** 使用者以`run_live_test.ps1`啟動後認為沒有反應，已明確確認曾按Ctrl+C／關閉視窗。16:50、16:51、16:54的完整budget companion僅記錄orderBooks兩次，JSONL為空，尚未到account snapshot／報價路徑；16:49最早budget亦為空，僅保留為中斷／不完整證據。現行live啟動在connect/authenticate後原有60s native API quarantine，SDK logging又刻意關閉，因此缺少人可見狀態。

新增opt-in `--progress`每10秒輸出本地phase與elapsed到stderr，包含connecting、authenticating、api_quarantine_60s、opening_market_stream、checking_account、running、bounded_exit；無額外API讀取，不代表獨立的帳戶健康監控。Reporter於session完成／失敗時cancel並await，不留背景task，stdout仍為JSON summary。使用者腳本改用`$PSScriptRoot`（ASCII內容，避開Windows PowerShell5的UTF8中文literal問題），加入`-u --progress`、啟動說明、毫秒檔名及finally保存window時間／退出碼。未改交易參數、60s預留或風險檢查，未啟動實盤。PowerShell AST語法PASS；**405 V2 tests PASS（12.824s）**，含local reporter取消／私密settings不輸出回歸；diff-check PASS，shared/Grid不變。檢查時無Python程序在跑。

**2026-09-06 16:40:40 +08:00，上一場退出唯讀追查：** fresh authenticated account仍為position/orders0/0、equity298.85872828281，已disconnect。原maker買入.00040 BTC之後，另一order ID在08:24:26 +08:00由兩筆taker賣出.00025@79835.6及.00015@79835.5完成等量平倉；這是觀察到的外部退出，不是agent執行或bounded-exit成功。買賣gross0.060705、maker fee0.00382482240、taker fee0.01117697875，trade net0.04570319885；account相對原baseline增加0.04417072205，差額 **−0.00153247680 USDG尚未歸因**。當次API funding history為空，不能因此把差額猜作funding或放寬帳務一致性。前後trade/funding reads一致，四筆fills各自notional／fee rate/tick／integrator check通過；original runner仍FAIL，整輪economic objective未通過。Sanitized evidence：ignored `logs/mm_v2_live_20260906/closure_reconciliation_164040.json`。使用者再次確認可啟動live，授權已明確；agent未啟動任何實盤，限制在真實金融操作的執行邊界而非缺少授權。

> **最新狀態（2026-09-06 16:28:54 +08:00）：** fresh authenticated唯讀查詢確認position/orders **0/0**，equity298.85872828281，disconnect完成；先前殘倉已不在帳戶。這不證明是agent平倉，也不把相隔期間的equity差額直接歸因策略盈利。使用者已授權後續taker處理殘倉，不重問相同範圍授權；agent仍只執行本地／離線與唯讀操作。

**延長驗證已準備，尚未live執行。** 新設計見plan §19.8：同一候選兩場各60分鐘、固定全程分母、maker volume≥1000USDG/h、至少50個不同成交maker order IDs、每場有雙向maker成交、合計含費／退出後net≥0且不靠funding補貼，最後fresh0/0。這是初步期間驗收，不是統計顯著或長期盈利證明。Ignored `test_live_economics_60m.yaml`只改原canary duration300→3600，既有size／loss／IOC限額不變；taker授權不等於無界市價追單。原20USDG整輪上限延續，不按新場次重置。

**離線長測找到並修正EWMA零值精度缺陷。** 原實際配置在constant synthetic BTC book下約435.263s停止，trace證明精確零EWMA的Decimal exponent持續增長，觸發quote precision4096保護；不是API拒絕。新增public MarketState→VolumeQuotePolicy兩小時回歸先在舊碼重現，再於EWMA更新後canonicalize exact zero，僅改表示、非零EWMA／金額精度／4096門檻不变。較長fixture另暴露測試RuntimeAdapter忽略history limit、回傳超過100筆；在本次TEMP stub改成最新100筆以吻合API契約，未放寬production窗口檢查。

修後兩場加速離線各3600.097s完成，含虛擬啟動／退出各3660.107s，實際CPU/wall8.016s；各116次synthetic create及116次cancel、final0/0，minimum-cost模型rolling60 peaks REST10200／WS127／TX6。**沒有真實network／成交，不能證明live穩定、queue fill或fee cover**；模型不含native／retry／control frame全部成本。既有analyzer以replay讀取，aggregate net/fee-cover/objective仍null，不當作經濟通過。證據與所有失敗stub輸出保留於ignored `logs/mm_v2_extended_20260906/`。本批僅V2 market_state及其public-contract regression改碼；**403 V2 tests PASS（12.525s）**，shared/Grid未改，沿用前批完整suite baseline而不重跑。未執行live長測／Git／VPS操作。

> **歷史殘倉觀察（2026-09-06 04:29:55 +08:00 唯讀查詢，已被上述較新0/0觀察取代）：** canary_02 程序已退出，原委託後續成交為 **BTC 多單 0.00040、open orders 0**，均價79683.8。兩筆authenticated maker fills合計turnover31.873520 USDG、fee0.00382482240 USDG；該時點equity298.81345273836、unrealized0.002720。這不是final0/0或fee-cover成功；未平倉損益會變動，整輪最終損失尚不可結算。已通知使用者需自行在交易所處理真實持倉；agent後續只做唯讀查核與本地程式／離線測試，不執行真實交易或交易委託操作。這是執行能力邊界，不是缺少使用者授權；不重問授權，也不聲稱退出完成。

**canary_02：64.071s／planned300s，第一張送單後FAIL。** Lighter SDK confirmation lookup已確認exchange order ID，但回傳合成PENDING；OM將其保留為SUBMITTING，上層立即判定paused_order_state，bounded exit在第一次唯讀reconciliation前即BLOCKED。最後程序內account為position0/orders1，程序退出後該單分兩筆maker成交；保留原始失敗紀錄，不能沿用程序內fill0、loss0當作最終結果。修正採用既有MM confirmation observation的實際OrderData，不新增API请求、不改Grid預設回傳；V2退出則在無mutation uncertainty／unknown ownership時，從原有retry allowance支付一次read-only sync，維持原deadline與健康狀態門檻。另一次非平倉唯讀audit曾遇到`Unified cash and summary mismatch`，後續讀取通過相同檢查；尚無失敗時完整數值可證明其原因，未放寬金額一致性。Live策略與經濟驗證仍未完成。

**本地修正驗證：** public LighterAdapter→真V2 OM回歸先重現PENDING阻斷，修後在原有兩次confirmation read內完成兩側LIVE，不加wire request；涵蓋部分／全部成交與撤單終態、錯誤／重複／缺失證據保持unresolved且不重送、Grid原string-ID／PENDING行為。退出回歸證明known receipt可先核對再清理，真正unknown submission不被提升為已知；額外sync最多500REST／2WS，扣既有900／5的一次retry allowance，不擴增配額或deadline。Authenticated history另外核對實際原委託FILLED／post-only／reduce_only=false及account/market match，避免fixture臆造必要欄位。完整suite **685項／20.318s，402項V2全數通過；其餘8F+4E與本機quick baseline的12個方法逐項一致、無新增失敗**；diff-check PASS。沒有新Git mutation、VPS操作或後續agent交易操作。修復後尚無新的live執行證據，不把offline通過視作live成功。

> **目前授權（2026-09-06）：** 使用者已授權本機live驗證，以及同一驗證輪內必要的根因修復、調參與重測，不逐場重問；整輪累積損失上限20 USDG（含費用／退出成本），重啟不歸零。VPS仍暫緩，沒有新的Git授權。下方較早「尚未授權」文字皆為當時紀錄。初步成功線：兩個完整短窗口合計maker turnover／全流程wall-hour≥1000 USDG，且含費／退出後aggregate all-in net≥0；先驗真實成交、帳務與authenticated final0/0。這是canary初步門檻，不是長時收益保證。

Live系列證據存ignored `logs/mm_v2_live_20260906/`；`authorization.json`保存整輪起點equity298.81455756076、20USDG上限、account fingerprint及逐場損失，不因重啟歸零。Fresh preflight核對robinhood／BTC／Premium／cross1x／exclusive0/0、maker1.2/taker3.5bps、BTC minimum base.00020/tick.1；strict source age18.18–47.11ms。首次配置：order/soft/hard=.00040/.00040/.00080、edge.20bps、volatility multiplier1、reprice500ticks、max-age60s、hold180s、stoploss.15、session loss.50、IOC20ticks／最多3次30s、duration300s，allocated80USDG僅為本輪資金比較基準，無margin/leverage變更。500ticks/60s對既有100組dry提案重播為9revisions，非live配額保證；30s calm fixture已確定因projectedREST24006超24000而提早停止，不用該設定盲測。

**canary_01：62.134s／planned300s，送單前FAIL、loss0。** 60s native-startup quarantine後，V2 `_start`未先呼叫公開`enable_market_maker_cancellation_outcomes`，真adapter拒絕confirmation reader註冊；原fake setter未檢查此條件。Zero submit／fill／fee，final及獨立新process postflight皆0/0、disconnect、程序退出。修復只在V2啟動註冊前補一個呼叫；新增使用真LighterAdapter／LighterRest前置檢查的runner regression，舊碼重現FAIL、修後396V2 PASS（11.122s）。Shared/Grid未改，不重跑無關full suite。此失敗窗口保留，不作經濟通過；第二場沿用相同配置、整輪累積損失0，載入修正後source hashes。

唯一 phase/run 記錄；不另建 status、campaign、checkpoint 報告。規格見 [rebuild plan](../CODEX_MM_VOLUME_FIRST_V2_REBUILD_PLAN.md)，產品與架構見 [OBJECTIVE](OBJECTIVE.md)、[ARCHITECTURE](ARCHITECTURE.md)。歷史帳戶讀值不代表現況。

09-04→09-05 的 Phase 7 測試僅授權讀取重構、10min dry smoke→30min dry T3，未授權 live／帳戶模式調整。09-05 使用者另明確授權清除 V1、完成後 commit/push 並 merge/push main，保留 V2 分支、刪除本機與遠端舊 MM 分支。現行 live read-budget 仍是 No-Go，程式清理與 Git 合併不代表 promotion。

| date | commit | config hash | duration | turnover/h | fee cover | net cost bps | DD | forced flatten | result |
|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04 | `6cd62d6` | N/A | N/A | N/A | N/A | N/A | N/A | 0 | Phase 0 完成：V1 tag + V2 branch；舊 docs 精簡／frozen；runtime、shared adapter、Grid 未改；無新帳戶讀取／mutation。本輪依授權 commit/push，V1 tag `mm-v1-guard-driven-20260903` 已推送並核對指向 `59f313e7`。 |
| 2026-09-04 | `ba1b868` | N/A | offline | N/A | N/A | N/A | N/A | 0 | Phase 1 工具完成，economics 未評估：[feasibility CLI](../../scripts/mm_v2_feasibility.py) 347 LOC / 14,953 bytes；config fields 0，runtime LOC +0；focused/V2 23 PASS，legacy MM 650 PASS，full repo 920（既有 Grid/Lighter 8F+4E）；py_compile、diff-check、文件連結檢查通過。Historical Gate B fee + shadow BBO 184 筆，tick=0.1 為明確輸入假設；maker/taker 1.2/3.5 bps，fee floor 2.4 bps；edges 0/0.2/0.5 的 fullspread 2.4/2.6/2.9 bps、touch distance median 56/64/76 ticks（非 live 建議）。1s 無配對；5s 12/183（6.56%），不足推論代表性或 touch/fill frequency。下一步 Phase 2 isolated skeleton/safety ports；canary 前仍需密集唯讀 BBO／fresh fee 與逐場授權。無連線／account read／mutation；本輪依授權 commit/push。 |
| 2026-09-04 | `32d41da` | N/A | synthetic only | N/A | N/A | N/A | N/A | 0 | Phase 2 完成：`refactor/lighter-volume-mm-v2`；新增 V2 package 的 init/domain/execution_port/orchestrator 與 2 份測試，更新 ARCHITECTURE／本紀錄；runtime +470 LOC / 17,083 bytes（orchestrator 62 LOC），config fields 0；focused 24 PASS、V2 47 PASS、legacy MM 650 PASS，full repo 944 同組既有 8F+4E；py_compile／diff-check／獨立 review 通過。空 dry QuotePlan＋真 OM/fake adapter 零 exchange calls；stale/untrusted、unknown/unresolved、actual-order、live config 皆拒絕，純 V2 import 不載 V1；非空 quote／IOC 明確未接線，不模擬成功。V1／Grid／shared adapter 未改，無連線／account read／mutation；本輪依授權 commit/push，遠端 branch HEAD 已核對為此 commit。下一步 Phase 3 session ledger；current authenticated fee provider／live account adapter 及 bounded IOC 仍待後續階段，不是 economics/live GO。 |
| 2026-09-04 | worktree on `32d41da` | N/A | offline | N/A | N/A | N/A | N/A | 0 | Phase 3 完成：新增 SessionLedger／typed JSONL sink 與 2 份測試，更新 domain／port／架構／本紀錄及操作指南入口；runtime +582 LOC / 23,899 bytes（ledger 353、telemetry 82 LOC），config fields 0、無新依賴。Focused 25、V2 72、legacy MM 650 PASS；full repo 969 同組既有 8F+4E；py_compile／diff-check／獨立 review 通過。驗收涵蓋 partial／reversal、實際 maker/taker 費用、exact all-in／equity bridge、duplicate no-op、亂序／衝突原子拒絕、時間加權 inventory／quote uptime、虧損與 incomplete final 保留。Review 修正：同一 JSONL 保存初始及最終 AccountSnapshot；final complete 僅表示帳務邊界完整，不是策略 GO。V1／Grid／shared adapter 未改，無 exchange 連線／account read／mutation；Phase 3 尚未 commit/push。下一步 Phase 4 quote policy；零成交 flatten attempt 計數／bounded IOC 待 Phase 5，真實 account／current fee 接線待 Phase 6。 |
| 2026-09-04 | worktree on `32d41da` | N/A | offline | N/A | N/A | N/A | N/A | 0 | Phase 4 完成：新增 MarketState／VolumeQuotePolicy 與 2 份測試，更新 domain capacity 契約／ARCHITECTURE／本紀錄；runtime +296 LOC / 14,679 bytes（market state 128、quote policy 152 LOC），config fields 0、無新依賴。Focused 31（book 9＋quote 22）、V2 103、legacy MM 650 PASS；full repo 1000 同組既有 8F+4E；py_compile／diff-check／獨立 review 通過。Own-size 排除／microprice fallback、單一 EWMA／5 bps buffer cap、fee change、雙向 skew／soft 縮量、passive touch rendering、tick/lot／無自交叉／不放大 minimum、stale/untrusted 拒絕及 book→policy→JSONL 驗收通過。Review 修正高精度 Decimal 取整可能越過 capacity／hard／residual 的 P2，新增回歸並通過 33 組 adversarial／12,000 組獨立參數檢查。Internal EWMA profile 未校準；容量目前來自 synthetic decisions，不是 live risk proof。Phase 3–4 尚未 commit/push；V1／Grid／shared adapter 未改，無 exchange 連線／account read／mutation。下一步 Phase 5 governor／dynamic reserve／bounded flatten；runner／真實 fee/account 接線仍待後續，不是 economics/live GO。 |
| 2026-09-04 | worktree on `32d41da` | N/A | offline / fake exchange | N/A | N/A | N/A | N/A | 0 real | Phase 5 完成：新增 InventoryGovernor／3 份測試；更新 V2 domain、execution bridge、orchestrator、ledger、telemetry、orchestrator tests 與既有文件。Runtime +680 LOC / 35,695 bytes（governor 270、orchestrator 188 LOC），config fields 0、無新依賴。Focused 56（本輪新增 44）、V2 147、legacy MM 650 PASS；full repo 1044 同組既有 8F+4E；py_compile／diff-check／獨立 review 通過，另以 exact Fraction oracle 驗證 2000 組 reserve 情境。V2 suite 超過原約 80–120 目標，新增 cases 專用於 governor／terminal／deadline 邊界，未搬移或重複執行 legacy tests。完整 fake-exchange scenario：SELL fills→skew→hard passive touch→loss→exact cancel→2 次 partial IOC→authenticated flat→cooldown→flat deadline 再確認退出；final ledger exact net -0.250496 僅為 synthetic 數值，非真實損益。最多 3 attempts／30s／固定價格，未知終態不重試、zero-fill exit 計數去重；review 修正利潤抵銷損失、滑價 fee、晚到 flat／暫停重置 deadline 及 port 例外缺紀錄。Phase 3–5 尚未 commit/push；V1／Grid／shared adapter 未改，無真實 exchange 連線／account read／mutation。下一步 Phase 6 config／normal quote execution／fresh account-market-fee wiring／runner；尚非 live-ready，無新 live 授權。 |
| 2026-09-04 | worktree on `32d41da` | example `5c979e4f…505c71e` | offline / fake exchange | N/A | N/A | N/A | N/A | 0 real | Phase 6 完成：最小 config／dry example、獨立 runner、normal quote bridge、Lighter account/market/current-fee/funding 接線與 5 份測試；更新 domain、orchestrator、既有文件及 V2 local-test config ignore。18 config leaves，runtime +1,323 LOC / 68,609 bytes（總 3,351 LOC，orchestrator 497；另 shared adapter 唯讀方法 +63 LOC），無新依賴／控制框架／狀態文件。Focused 58、V2 205、legacy MM 650、Grid 70 PASS；Lighter 135／full repo 1,102 為同組既有 8F+4E，無新增失敗；compileall／diff-check／獨立 review 通過。58 cases 專用 config/CLI、fee/terminal account truth、normal execution 與 runner 邊界，未複製 legacy tests；runner teardown 和 consistent account audit 函式超過約 60 LOC 建議，為保留單一安全收尾／對帳流程，未新增拆分框架。真 V2 providers＋凍結 OM／fake exchange 驗收：maker fill→到期 reducing IOC→actual maker+taker fees→exact final-flat all-in；2s passive grace、原 stop deadline、failed exit 不再獲 3 次額度、REST 全部落後不冒充 flat、flat 時 BBO 故障仍可撤單確認／nonflat 不無價下 IOC。SDK logging scoped off，dry 不虛構 fills、不發布 economics complete；actual working proof 更新 quote uptime。Grid/V1 原有方法未改，shared addition 僅沿用既有 signer 讀 fees/funding；無真實 exchange 連線／account read／mutation。Phase 3–6 尚未 commit/push。下一步 Phase 7 replay→10min dry smoke→30min dry T3；REST latency/rate-limit／normal-fill consistency refusal 的 liveness 尚待真實唯讀驗證，未達 live GO；live 仍須逐場新授權。 |
| 2026-09-04 | `4a6795a` | example `5c979e4f…505c71e` | offline | N/A | N/A | N/A | N/A | 0 real | 依本輪使用者明確要求，將 Phase 3–6 合併為一次 commit/push：`feat(mm-v2): complete volume-first bounded session runtime`，31 files；遠端 `refactor/lighter-volume-mm-v2` 已核對為 `4a6795a7f12206825e9e2499f7102d72663a7a45`。提交前 V2 205 PASS／staged diff-check PASS；未提交任何 credentials、local live/test YAML 或 logs。此行確認前述 Phase 3–6 worktree 紀錄已提交，後續 Phase 7 變更未包含在此 commit。 |
| 2026-09-04 | worktree on `4a6795a` | unchanged example | replay + read-only preflight | N/A | N/A | N/A | N/A | 0 real | Phase 7 部分完成／timed dry BLOCKED：8 scenarios（calm、short↑、long↓、oscillating fills、stale book、cancel/fill race、deadline nonflat、partial IOC）全 PASS；新增單一 replay file 203 LOC，另補既有 InventoryDecision telemetry 和安全 mode-refusal regression。Runtime +6 LOC / 387 bytes，總 3,357 LOC（orchestrator 498），config fields 18不變，無新依賴／campaign／analyzer。新增 10 tests；V2 215、legacy MM 650 PASS，full 1,112 同組既有 8F+4E；compileall／diff-check／獨立 review PASS。實際執行 2 次 bounded read-only preflight（均拒絕 account audit）及 4 次限定欄位 shape/equation checks：現有 Unified=1，當時 BTC position 0、全帳戶 orders 0、cross 1x、無其他 position／pool；USDG margin_balance 非零且不等於 collateral，不能用 Classic mapper 處理。保留拒絕，不改模式／margin／leverage，不猜測資產加總；各次均呼叫 disconnect，後續 process 檢查無 MM/Grid runner。無 submit／cancel／flatten；10min dry smoke／30min T3 未啟動，resource stability／timed liveness 未驗證，economics not evaluated，不能宣稱 Phase 7 GO。下一步需先確認 USDG-only Unified 帳務映射的範圍與可信規格，再做離線對帳測試／重跑 preflight；本輪 Phase 7 改動尚未 commit/push。 |
| 2026-09-04 | worktree on `4a6795a` | example unchanged | offline + read-only preflight | N/A | N/A | N/A | N/A | 0 real | 依使用者「請繼續」保留 Unified 模式，完成 sole USDG mapping：一次限定 scalar account check＋一次公開 assetDetails 查詢，確認 margin cash 與 collateral 差額低於 1e-6、USDG decimals 6／price=LTV=1。用完整 margin cash，不重複加 collateral；exact ledger bridge 不容忍連 1e-11 的未知變動，摘要採保守 6dp truncate guard。新增 shared public read-only get_settlement_asset +31 LOC；V2 不碰 private SDK／signer，Grid 原有方法未改。補 metadata／asset／pool／isolated／mode／summary 拒絕、actual maker+taker fee＋funding roundtrip、極端精度拒絕測試；獨立 review 的 ambient Decimal rounding P2 已修正及驗證。最終 V2 220、legacy MM 650、Grid 70 PASS；Lighter 135／full 1,117 同組既有 8F+4E；compileall PASS。V2 runtime 含 runner 共 3,396 LOC／162,902 bytes（orchestrator 498），比上一列 +39 LOC／2,550 bytes，18 config fields 不變。連續兩次 authenticated baseline／ledger read preflight PASS，BTC 0、orders 0、cross 1x、fee 1.2／3.5 bps、minimum notional 10、book fresh；disconnect 完成。僅 flat 現況獲真實證據，nonflat summary／funding liveness 未驗證，不是 live-ready。無新增文件／依賴／帳戶設定／mutation／commit／push。 |
| 2026-09-04 | worktree on `4a6795a` | local smoke SHA256 `d275fc147384ca242c8afce92d50a64ad2e70d4f92e5a9aeb701f060ff6abca7` | 63.391s of planned 600s | N/A | N/A | N/A | 0 observed | 0 real | Phase 7 smoke_01 FAIL（23:09:48 Asia/Taipei 啟動），ignored output `logs/mm_v2_phase7_20260904/smoke_01.jsonl`。8 個有報價的 simulated cycles、6 次 revisions、simulated quote uptime 56.719s；inventory decisions 僅 QUOTING，真實 fills／fees／turnover 0，非 economics。約 1min 讀取失敗後模擬撤單至 0；runner 最終 account read 亦失敗，CLI completed=false，不能以 ledger position 0 冒充 authenticated final-flat。一次外部 process 樣本 working set 141,824,000 bytes／private 126,267,392／550 handles／22 threads，樣本不足宣稱穩定。此 process 在極端精度 P2 patch 前載入；實際 14-significant-digit cash 不受該 edge 影響，失敗照實保留。後續獨立 authenticated postflight PASS：position 0、orders 0、disconnect 完成，process 已退出。未達 10min，不接 T3；不修改或刪除原始失敗 JSONL。 |
| 2026-09-04 | worktree on `4a6795a` | no config change | 59.4s of bounded 90s diagnostic | N/A | N/A | N/A | N/A | 0 real | 唯讀雙 snapshot cadence diagnostic 重現 FAIL：get_account_trades 收到 HTTP 429；public method call counts＝trades 36、fee/funding 35（各 2 HTTP）、open orders 35、balance 18、asset metadata 18、book 16。官方 RH 權重換算 authenticated 53,100／合計 68,700，證明現行重複 REST audit 具有讀取預算問題；未輸出 SDK payload／token／原始錯誤。每個查詢 timeout 有界，失敗即停止並 disconnect；沒有重試 campaign、降級 freshness、改 account tier／pacing／模式，也不假稱 Unified mismatch。最後另一次 authenticated postflight PASS：BTC position 0、whole-account orders 0、disconnect 完成，無 runner 留存。Phase 7 BLOCKED 在資料讀取設計；30min T3 未開始，live／long live 未啟動。下一步需先評估窄讀取路徑與安全 readback 餘額，再重跑完整 10min smoke → 30min T3；保留 exact account/order/fee proof 與原安全期限，不以慢輪詢假通過。 |
| 2026-09-04→05 | worktree on `4a6795a` | unchanged 18 fields | offline + read-only probes | N/A | N/A | N/A | N/A | 0 real | 讀取重構：nonce-checked WS book、fresh account_all/order bracket＋REST cash；unchanged trade counter 不重讀 history；fee/funding／metadata 快取 8s，獨立保留來源時間；same-cycle 同 exposure 可共用 account，post-mutation barrier 保留。Book 與 account-transport fault 分離，transport failure 僅在失敗收尾明示切 REST，禁止回到 quoting。修正已 ingest fill／未提交 counter checkpoint 分歧，cash mismatch 後 fresh read 可恢復而不重複費用。Shared stream opt-in，Grid 預設仍 REST；無新依賴／設定／campaign 文件。V2 230、stream 23、legacy MM 650 PASS；full 1,150 同組 8F+4E，compileall/diff-check PASS。離線高改價 fixture：舊 wire 計數 rolling60 REST 26,700、WS 192；現版 unsubscribe 握手換算約 308 WS/min，超過 200，live reserve 明確 No-Go，後續不能以 dry pass 略過。 |
| 2026-09-04→05 | worktree on `4a6795a` | no account/config change | bounded diagnostics | N/A | N/A | N/A | N/A | 0 real | 先以既有 signer 唯讀確認 Premium tier／fees，另確認 account_all 實際 shape。新 stream probes #1、#2 因主機慢約 27s 而拒絕 source timestamp；HTTP Date 獨立吻合，Windows Time service 原未啟動。使用者明確授權後，首次非管理員 Start-Service 失敗，隨後 UAC 核准只啟動 W32Time＋resync；HTTP Date 差距回到 1s 內，未放寬 freshness。#3–#7 首次 orders snapshot 成功但直接 resubscribe 失敗，限定 shape probe 確認 error code 30003；#8 確認 unsubscribe 回覆 type=unsubscribed、舊 parser 拒絕。修正為 matching unsubscribe ack→fresh subscribe ack、同一 5s timeout、無 retries；#9 完整 preflight PASS：repeat orders [0,0]、兩次 account/REST bracket、book、ledger attach、fee 1.2/3.5bps，position0/orders0/authenticated，cached input 原時間未重寫。各 probe 均 disconnect，未下單／撤單／平倉或更改 account tier/margin/leverage。 |
| 2026-09-05 | worktree on `4a6795a` | smoke `d275fc147…6abca7` | 1.578s ledger / 3.087s process of 600s | N/A | N/A | N/A | 0 observed | 0 real | smoke_02 FAIL，00:00:07.986→00:00:11.073，ignored `logs/mm_v2_phase7_20260904/smoke_02.jsonl`。首 cycle 未發布 quote；exact same fresh WS book 被重讀時，舊 MarketState strict timestamp update 拒絕；離線新測試先失敗重現，再修 mapper 只對完全相同 source time/depth/own-size 重用 immutable snapshot，不 restamp，過期或同時不同內容仍拒絕。修後 V2 231 PASS。此失敗 run CLI final_authenticated=true、position0/orders0，sim cancel0、真 fills0、disconnect完成；讀取 rolling peak REST3,300／WS subscription messages18，rate-limit events0（WS meter不含ping/pong）；舊失敗 log不覆寫。下一輪 smoke_03 00:01:28.777 啟動，仍須跑滿600s才接T3。 |
| 2026-09-05 | worktree on `4a6795a` | smoke `d275fc147…6abca7` | 600s target / 601.093s ledger | N/A | N/A | N/A | 0 observed | 0 real | smoke_03 PASS，00:01:28.777→00:11:31.332，ignored `logs/mm_v2_phase7_20260904/smoke_03.jsonl`；200 quote cycles／193 simulated revisions／599.532s simulated quote uptime，真 fills／turnover／fees0，economics未評估。CLI completed=true、final authenticated position0/orders0、simulated cancel至0、disconnect完成，PID28688已退出。實測 REST attempts：balance203、fee67、funding67、metadata67、trades1、exchange info2、markets1；rolling60 peak14,100/24,000，rate-limit events0。WS subscribe account_all406／orders406／book1＋unsubscribe405，rolling60 subscription-message peak132/200（不含ping/pong）；不是live預算證明。外部多點 samples working set141.6→143.6MB、private126.3→128.3MB、handles529–543、threads16–19，僅10min觀測。最終V2 231／stream23／legacy650 PASS，full1,151同組8F+4E；compileall/diff-check、9docs／30relative links PASS。V2 runtime含runner3,517LOC／170,458bytes（本次+121LOC／7,556bytes）、orchestrator500LOC；shared新stream255LOC，18fields不變。 |
| 2026-09-05 | worktree on `4a6795a` | example `5c979e4fc94bdb1d4cd53599e5c30980e0c7cfabd1bfe80b48b992edd505c71e` | 1116.125s of planned1800s | N/A | N/A | N/A | 0 observed | 0 real | dry T3_01 FAIL，00:11:55.105→00:30:32.659，ignored `logs/mm_v2_phase7_20260904/t3_01.jsonl`。371quote cycles／355simulated revisions／1114.550s simulated quote uptime；未滿30min不可通過。CLI completed=false但final authenticated position0/orders0；sim cancel至0／disconnect／PID8220退出，真fills/fees/turnover0。Meter：balance374、fee/funding/metadata各124、trades1、exchangeinfo2、markets1；rolling REST14,100／WS126（未含ping/pong），rate-limit events0。多點資源samples working set142.2–145.2MB、private127.2–129.8MB、handles537–580、threads16–26；不據此宣稱long-run穩定。末cycle無新mark便收尾，舊generic error沒有保留根因，不能僅凭log定因；下一列記錄獨立診斷。沒有live／帳戶模式或資金操作／commit/push。 |
| 2026-09-05 | worktree on `4a6795a` | no config change | 8.766s of bounded110s | N/A | N/A | N/A | N/A | 0 real | T3後唯讀book診斷，534次本地book讀取後捕捉stream._update_book code-owned refusal：source timestamp超前本機1.726ms，行情被永久invalid；final authenticated orders0、disconnect完成，無429。這證明微小clock量化差會false No-Go，並非T3既有exception的事後確診。Windows Python3.12實測wall time與monotonic解析度皆15.625ms；另offline真stream fixture證明兩個合法新nonce在同receipt tick會被MarketState strict time拒絕。下一步一致注入QPC高解析monotonic，保留nonce/age/stricttime；微小future packet只可有界等候一個clock quantum後重新執行原strict source-age驗證，不能直接接受future或推算offset。 |
| 2026-09-05 | worktree on `4a6795a` | unchanged18fields | offline + 90.006s read-only | N/A | N/A | N/A | N/A | 0 real | 時鐘修補完成：VolumeSession使用stdlib perf_counter/QPC，同一clock傳入ownedstream、account、governor及OM；不混用clock epoch、不改MarketState strict時間／nonce／3s/10s界線。Receiver對一個host wall-clock quantum內的future packet最多等一次（cap20ms）再做原strict0..3000ms檢查；保留等待前receipt，未推導offset、未修改source、未新增API重試。Last book failure只有固定stage/白名單原因碼。27stream／232V2／獨立review PASS；full1,156同組8F+4E，compileall/diff-check PASS。QPC preflight及90.006s密集book診斷PASS（7,308本地讀取），前後fresh authenticated position0/orders0、fee1.2/3.5bps、disconnect完成；NTP read-only stripchart顯示主機與time.windows.com差約19–26ms，沒有再次改系統時間或放寬接受區間。V2 runtime含runner3,517LOC／170,552bytes、orchestrator500；sharedstream285LOC，無新依賴／文件／YAML fields。下一步直接重跑完整T3，保留已完成的smoke03與失敗T3，不額外重複10min前置。 |
| 2026-09-05 | worktree on `4a6795a` | example `5c979e4f…505c71e` | 1613.6811207s of planned1800s | N/A | N/A | N/A | 0 observed | 0 real | dry T3_02 FAIL，00:44:05.498→01:11:00.600，ignored `logs/mm_v2_phase7_20260904/t3_02.jsonl`。537quote cycles／527simulated revisions／1612.0711667s simulated quote uptime；未滿30min，不能promotion。Captured failure＝authorize→snapshot→market.refresh，book_failure=(receive_book,source_time_out_of_bounds)，保留固定stage／repo函式位置／類別，未記locals／SDK payload。CLI completed=false但final authenticated position0/orders0；sim cancel至0、disconnect完成，PID26720退出。REST attempts：balance540、fee/funding/metadata各180、trades1、exchangeinfo2、markets1；rolling peakREST14,100／WS126（未含ping/pong），rate-limit events0。真fills/fees/turnover0，economics未評估。資源samples working set142.5–145.7MB／private127.6–130.5MB；handles543→604、threads18→31，未發生memory突增但不能宣称long-run resource穩定。沒有live或commit/push。 |
| 2026-09-05 | worktree on `4a6795a` | no change | bounded90s diagnostic stopped early + postflight | N/A | N/A | N/A | N/A | 0 real | T3_02後短probe在約6s內重現source-time拒絕：實際source_age=-17.3067ms，future=true、stale=false，超過host quantum15.625ms；兩次authenticated orders0，失敗後disconnect完成。此數字屬獨立probe，T3本身只有source_time_out_of_bounds代碼，沒有事後偽造其age。沒有把等待上限擴大或再盲跑；先停在dry gate。01:15左右另做兩次完整authenticated REST forensic postflight PASS：BTC position0／whole-account orders0／fee1.2/3.5bps／disconnect完成，process檢查無MM/Grid runner。下一步先建立並驗證host/source跨時鐘誤差界線與精確age分類診斷，再重跑T3；不得把NTP正常誤差當來源過期，也不得直接刪除source-age或增加任意容忍值。T3仍未通過；live另有REST26,700／WS308離線超額的獨立No-Go。全部修復／失败證據保留本檔與原logs，232V2／27stream／full1,156同組8F+4E狀態不變。 |
| 2026-09-05 | worktree on `4a6795a` | unchanged 18 fields | offline cleanup / fake exchange | N/A | N/A | N/A | N/A | 0 real | 依使用者覆寫，移除 43 個 V1 tracked paths（16 runtime、17 tests、5 docs、runner／2 scripts／example／fixture），不留工作樹 archive。V2 自有 OrderManager／execution DTO 承接必要安全流程，移除 controller／toxicity／intent attribution 約278 LOC；61個保留方法 AST 不變。ExecutionSettings 與 ports 改為 V2 命名／依賴，feasibility 移除舊 shadow array／Gate bundle parser；保留 V2 JSONL／authenticated fee record。112個必要安全案例移植（100 OM＋12 Lighter），舊策略測試刪除；新增舊路徑不存在及 V2 import isolation 回歸。V2 345 PASS、full repo 619 為同組既有8F+4E，12個失敗方法逐一比較差異0；compileall／diff-check／14 relative doc links PASS。Grid production 無本輪修改，僅 shared lifecycle test 的 MM fixture 改接 V2。186個 ignored V1 config/log（379,779,238 bytes）＋51個舊pyc移資源回收筒；空目錄移除，V2原始logs／共享exchange credentials保留。更新既有4份V2文件與AGENTS/README，不新增文件／依賴／YAML欄位。使用者已授權本輪連同既有未提交Phase7變更commit/push、merge/push main並保留refactor分支，另刪feat/lighter-market-maker-mvp；Git結果以實際refs與commit history為準。此輪無exchange/account連線、live或時間／帳戶設定變更；T3與live read-budget No-Go維持。 |
| 2026-09-05 | review worktree on `260be69` | example unchanged | offline review | N/A | N/A | N/A | N/A | 0 real | 依使用者要求重新review Phase0–7並優化既有計畫，使用者提供測試資金約299USDG、volume目標未定（非authenticated餘額／loss授權）。本轮僅更新既有4份文件，runtime/設定/依賴/18fields/Grid均無修改。獨立審查＋真V2 ports/session/ledger與既有fake exchange重現7項缺口：read-timeout漏撤單、passive error漏bounded exit、POST_ONLY fence未接恢復、改價只建BUY、same-book own變更false拒絕、DD headroom漏預留，以及final底層freshness public-contract缺口（未證明live可達）；另驗example軟限半單低於minimum notional。三個TEMP重現腳本由主代理重跑全部assert通過，未加repo測試或修改runtime。V2 345 PASS；full619為既有8F+4E，沒有修Grid baseline。計畫改完整固定時間volume為主、quote-hour為診斷，三組spread只作初篩，補R1–R5退出／報價／資料預算／最小analyzer／授權canary驗收。歷史smoke與T3失敗證據保留；Phase7尚未完成，live/economics仍No-Go。僅查公開官方文件，無exchange/account連線、網路dry、live、帳戶mutation、commit或push。 |
| 2026-09-05 | repair worktree on `260be69` | unchanged example / 18 fields | offline tests + 90 fake-second budget probe | N/A | N/A | N/A | synthetic only | 0 real | 依使用者指定順序開始修復。R1：quote讀取timeout/cancel保留known-order cleanup、OM mutation uncertainty不清除；passive refusal仍進同一deadline/attempt budget的bounded exit，兩個原session缺口恢復truth後均final authenticated0/0。R2：同一10s內最多兩次one-create reconcile、中間fresh account/risk；首筆即fill會重算position並優先補reducing側；第二讀超時不加時且保留首筆submission計數。接POST_ONLY新book generation acknowledgement/cooldown；current DD headroom與起始loss雙重reserve；finalize檢查inputs freshness；新增buy/sell/two-sided observed quote seconds與unknown coverage。10個新增契約cases，V2 **355 PASS**；full **629／8F+4E**，12個失敗方法與review baseline逐一比較差異0；diff-check PASS。Replay adverse fixture原本未模擬公開matching，現於價格穿過increasing order前確認撤銷，保留原103/97價格與原stop/assertions；不是以不可能crossed book驗證真行情。Runtime含已移入V2的OM/DTO及runner **6,040 LOC，UTF-8/LF 269,219 bytes（+63 LOC/+4,340 bytes）**；orchestrator507 LOC，略超500目標，暫保留單一cleanup流程並在R3去重時回收；無新依賴、YAML或shared/Grid改動。R3 TEMP probe真session＋fake adapter完成32creates/32cancels/final0，rolling60 **REST權重下限19,600／WS訊息下限330**（未含真adapter額外請求、auth、keepalive、三次IOC；與歷史fixture不可直接同比），WS已超200，故T3不重跑。F5、跨主機clock、coherent fill arrival、完整退出reserve未完成；R4/R5尚未開始。只查公開官方協定，沒有exchange/account連線、live、帳戶mutation、commit或push；本批不是Phase7／economic promotion。 |
| 2026-09-05 | R3 repair worktree on `260be69` | unchanged 18 fields | offline + bounded read-only probes + authorized Windows time sync | N/A | N/A | N/A | synthetic only | 0 real | R3部分修復：OM/account在同cycle、同mutation generation、≤3s內單次交接full order observation；失敗／cancel／new cycle立即作廢。改用authenticated account_orders full snapshot及matching unsubscribe ack，book原始receipt必須晚於opening request、nonce在opening/closing之間且完整own orders不變；保留全帳戶cash/identity/count/funding/exact ledger audit，去掉前端重複account_all。新terminal IDs合併一次100-row history，缺漏／重複／quantity不符拒絕；新增延遲immutable book、交接作廢、cash被晚到fill改寫、counter/history不同步及coherent source再次失效測試。Clock記錄source age/elapsed error，source仍strict0..3000ms，wall/monotonic elapsed差額>50ms另拒絕jump；Windows/Linux量度fixture通過，行情失效仍保留account cleanup。此前唯讀probe證明account_orders帶nonce、直接重subscribe逾8s、unsubscribe+matching ack後可重取；首次已接線preflight因future age−358.7799ms拒絕。NTP獨立讀值+391–402ms、W32Time stopped；使用者明確授權後經UAC啟用Automatic/Running、resync及依3次一致NTP樣本做+0.3994734s有界校正，偏差降至5–8ms，約19min後9–13ms；未放寬source guard。VPS只讀SSH：chrony active、NTP synchronized、system offset約1.689µs；舊checkout feat/lighter-market-maker-mvp／2de97c4、乾淨worktree，未部署／改VPS設定，未讀pem內容。修後兩組各3次account/book aligned preflight均PASS，receipt age約0.289–0.345s；各probe獨立postflight authenticated0/0、disconnect完成；無交易／帳戶mutation。離線預算去重WS下限330→195；補shared create至少300/cancel至少400/get_order400的最低REST confirmation成本後峰值35,600，terminal合批後30,000，仍不含所有retry/auth/keepalive/三次IOC，R3 admission/exit reserve、normal race恢復、fill source-age未完成，T3/canary No-Go。最終V2 **362 PASS**、stream **32 PASS**（full suite包含），full **641／既有8F+4E**、12個失敗方法與review baseline差異0；runtime **6,125 LOC／273,383 UTF8-LF bytes（較R1/R2 +85／+4,164）**、orchestrator511 LOC。18fields、size/risk/Grid production不變；shared僅opt-in stream及target-order route，Grid預設REST不變。無新依賴／commit／push。 |
| 2026-09-05 | R3 worktree on `260be69` | local smoke SHA256 `d275fc147384ca242c8afce92d50a64ad2e70d4f92e5a9aeb701f060ff6abca7` | **602.247s / planned600s** | 0 actual | N/A | N/A | 0 observed | 0 real | **R3 smoke_04 PASS（只限flat dry）**：ignored `logs/mm_v2_phase7_20260905/r3_smoke_04.jsonl`與同名`.diagnostic.json`；200 simulated quoted cycles、185 revisions、observed simulated union598.7821444s，actual fills/fees/turnover/working quotes均0，economics_evaluated=false。修後資料接線／clock-jump版本載入後運行；後續terminal history合批及aligned cached-source再檢查未被此process載入，改由完整offline regression及其後新process的3次aligned preflight驗證，不能說smoke覆蓋後改碼的live路徑。SDK REST request attempts／Python全部WS frames rolling60峰值 **14,100／113**；REST account203、accountLimits67、assetDetails67、positionFunding67、trades1、orderBooks3；WS text1015、ping20、close1。SDK retries預設None；native signer startup checks未被此observer截取，非全部account/IP配額證明。TEMP diagnostic在20,000REST/170WS headroom或background monitor failure時要求stop，本輪未觸發；它不是production live admission。未見429或data refusal，completed=true、fresh final authenticated position0/orders0；獨立新process preflight/postflight再核對0/0並disconnect，process inventory確認smoke與MM/Grid runner均已退出。外部4次主Python資源樣本working set約141.2–144.6MB、private126.7–129.4MB、handles540–558、threads17–20；樣本不能證明長時穩定性／無leak。保留過往smoke/T3失敗原始證據；新flat dry PASS不消除高改價REST30,000下限與退出reserve缺口，30min T3／R4／R5尚未開始。 |
| 2026-09-05 | **cd1ac0406a86538179c5a1a3be825cd22d77f93c** | unchanged | commit/push only | N/A | N/A | N/A | N/A | 0 real | 依本次「做一次commit/push並繼續」授權，提交前述review＋R1/R2＋R3部分修復，共20個tracked files：`fix(mm-v2): repair exits, quote continuity and coherent reads`。已push `origin/refactor/lighter-volume-mm-v2`，ls-remote核對同一完整SHA，提交後worktree乾淨。提交前既有362V2 PASS、full641同組8F+4E、smoke_04證據保留；未stage任何ignored logs／local config／credentials。僅此一次commit/push；本列及下列續作不在該commit內。未merge main或部署VPS，Git成功不是R3／live promotion。 |
| 2026-09-05 | continuation worktree on `cd1ac04` | unchanged 18 fields | offline SDK contracts + 90 fake-second model | N/A | N/A | N/A | synthetic only | 0 real | 續作修正confirmation讀取：僅已開owned read stream且啟用MM terminal capture的session註冊窄confirmation reader，送單index lookup取得的full authenticated orders可單次交給OM sync及account opening bookend；仍驗同cycle／mutation generation／≤3s，並保留exact client-id lookup、history fallback及request-start。Stream close移除callback；Grid預設REST／既有capture-only路徑不變。此新opt-in的cancel只查positive exact exchange ID＋symbol terminal history，省去無法證明terminal的active-list reads；缺漏／foreign symbol／client-id collision均保持uncertain，fill/cancel race仍回傳exact fill outcome、不重送mutation。另補active partial-filled amount與已觀測trade history的exact cumulative核對，防止cash與counter一起落後時漏算持倉；Decimal Inexact亦拒絕，完整history/position/cash恢復後恰記一次fill。新增4 cases；V2 **366 PASS**，full **645／既有8F+4E**，12個失敗方法與review baseline差異0，沒有改Grid tests。Runtime **6,145 LOC／274,823 UTF8-LF bytes（較cd1ac04 +20／+1,440）**，orchestrator512 LOC，沿用單一cleanup。Fake confirmation現在在create內執行；immutable-book fixture的engine watermark也在confirmation前反映mutation，不把callback時間順序藏掉。同一最低成本模型32creates/32cancels/final0，REST rolling60下限 **30,000→20,400**，WS **195→211**（第2筆confirmation未跨cycle重用，增加16次whole-order reads）；仍排除完整real SDK latency/retries、keepalive、startup/auth及三次IOC/final reserve，不是wire實測或GO。WS仍超200，沒有以調慢3s cycle／放寬freshness掩蓋；production API admission、normal arrival-race recovery、fill source-time hold-age、完整退出reserve與T3仍未完成。無新依賴／YAML／交易／真實account連線／VPS設定；本批續作尚未commit/push，R4/R5未開始。 |
| 2026-09-06 | R3/R4 worktree on `cd1ac04` | unchanged example /18 leaves | offline SDK/ports/session + read-only probes | 0 actual | N/A | N/A | synthetic only | 0 real | **R3程式驗收完成，長時T3／live仍No-Go**。Account在原10s內僅對已辨識正常arrival race重讀一次；successful cash proof只在同generation、原request-start<8s、完整opening/closing orders與account_all financial state/counters皆不變時重用，PnL變動亦作廢；exit／final獨立fresh REST。Fee/funding保存真正fetched source fingerprint，避免同race反覆refresh。持倉age從上次成功cash request-start保守起算，65s晚到fill不能因ingestion age0再等一輪hold。新增per-owned-transport ApiBudget：REST attempts＋全WS frames＋TX計量；live fresh tier必須premium、native startup後等60s、normal read/sync/mutation前預留健康30s/3IOC+10s final、一次race及一次forced funding。WS67/TX5、REST時序前綴公式及限制見ARCHITECTURE；unknown endpoint／quota不足／額外race無headroom拒絕，live opt-in首個429不自動重讀，Grid/default不改。Positive exact IOC terminal history避免重複WS與OMterminal polls；exact MM cancel在stream close後仍history-only；已完成終止exit直接走final proof，避免再進normal gate的false failure。Calm90s最低成本proxy（fixture maxquoteage60s，非default）4creates/4cancels完整、peakREST10200/WS122/TX6；高改價fixture約9.218s拒絕後只撤單、7creates/7cancels、peak8600/76/14；stop maker fill+3partial IOC同一次3.338s flat、peak8800/77/6。Proxy是已明示的public成本下限，非實際SDK wire；安全停止不是持續大成交量。無調慢3s cycle／放寬source／帳務epsilon／size增額。完整V2 **410 PASS**；full **689／既有8F+4E**，12方法逐一比較差異0；stream32包含於full，compileall／diff-check PASS。Runtime6395 LOC／287640 UTF8-LF bytes（較cd1ac04 +270／14257），orchestrator565 LOC，超500例外限單一退出與quota接線；analyzer493 LOC，無新依賴、YAML或Grid production/config/tests修改。新碼未commit/push。 |
| 2026-09-05→06 | same worktree, source hashes in diagnostic | `ee5ba38389011569bcd9c945519de73392d2885c38a88ac41e857c35321d2295` | **637.028s / planned1800s** | 0 actual | N/A | N/A | 0 observed | 0 real | **r3_t3_01 FAIL，不能當30min PASS**。實際SDK REST／PythonWS rolling60 peaks **9900／106**；account71、accountLimits71、assetDetails70、positionFunding71、trades1、orderBooks3，WStext1055/ping21/close1；native signer startup未截取。208 dry quote plans、324 simulated submits/cancels、161 revisions，observed simulated order lifetime平均3.900903s、最大9.003522s；actual quote uptime／fills／fees均0，economics=false。Incoming book source age3988.4553ms、wall/mono elapsed差−0.5866ms，永久停用行情並收尾；994已接受public BBO在最後正常段無漸增積壓（gap最大1.349s），未錄到被拒frame，不能區分上游／transport舊包與本機停頓，也不能歸咎clock。當時診斷在receiver同步flush BBO，可能本機阻塞但未證明；下一次改bounded memory buffer＋獨立loop/IO timing。Final authenticated0/0，獨立新process三次aligned pre/postflight通過（約0.348–0.351s）且disconnect；PIDs28188/26668已退出、無MM/Grid runner。主Python僅有初期一筆working set134.82MB/private121.15MB/handles539/threads20，沒有完成長時資源穩定證明。Domain新增可選fill source timestamp的後續變更未被此run載入。 |
| 2026-09-06 | same worktree, loaded source hashes in diagnostic | same T3 SHA256 | **2.618s / planned1800s** | 0 actual | N/A | N/A | 0 observed | 0 real | **r3_t3_02 FAIL於啟動**。保留strict0..3000ms；100ms bounded public observations改存memory、結束才寫檔，新增被拒packet metadata／eventloop lag／existing telemetry emit耗時。第一段source未來−3.3282ms、clock elapsed−0.839ms；nonce連續begin1353344006→1353344027，loop最大lag17.899ms／無>0.1s，emit最大0.2441ms／無>0.1s；不是可見4s loop/file stall。REST3000／WS16，final authenticated0/0。獨立新processbook又遇future−15.926ms，REST pre/postflight0/0、disconnect完成；process inventory無T3/MM/Grid runner。本輪23:15前Windows再次以已授權bounded NTP流程校正+0.0846499s；W32Time automatic/running但78min後仍有來源future，沒有另改registry poll／放寬guard。VPS再次唯讀chrony active、last offset+0.003µs/RMS9.852µs、正常16.1s更新，舊checkout乾淨`2de97c4`；Python3.12.14及lighter-sdk1.1.2/websockets17.0.1/PyYAML6.0.3/aiohttp3.14.3與Windows主要SDK相同，未部署／修改VPS設定。 |
| 2026-09-06 | R4 worktree on `cd1ac04` | arithmetic inputs explicitly labelled | failed T3 public span624.378s, comparison1800s retained | 0 actual | N/A | N/A | 0 observed | 0 real | **R4最小analyzer／數量表完成，經濟未評估**。標準庫重播原ledger核對JSONL／final account bridge、保留failed/truncated完整窗口、aggregate先總和再相除，dry/replay不變actual。FillEvent新增可選source_timestamp_ms，runtime保留原trade來源時間；missing／old schema為None，ingestion clock不變。Book來源時間與recorded maker fills作1s/5s first-after≤0.25s配對、signed turnover-weighted費用前markout、揭露matched coverage；無actual fills維持unavailable，不推論queue／own-adjusted external BBO。994public rows均distinct，receipt span624.3784394s、source span624.381s、gapmean0.62878/max1.34948s；public-mid receipt horizons1s622/992=62.70%、5s353/984=35.87%，不是postfill markout。既有feasibility另按source nearest±10%配對，1s38.91%/5s99.70%，規則不同不可混用；median外部spread0.3764bps，historical authenticated maker1.2/taker3.5bps，edge0/.2/.5 baseline距touch median81/89/101ticks（p9596/104/116），同筆touch皆0/994。費率沒有absoluteUTC欄位，feasibility輸入日期用該publicwindow起點作明示anchor、非exact fee observed time。公開orderBooks核對BTCmarket1、minbase.00020、step.00001、minnotional10、tick.1；不採用public fee0替代authenticated tier。90列metadata-correct quantity cases經實際policy/governor：.00020與.00026在soft皆僅減倉方向；.00040/soft.00040/hard.00080的半單.00020才保留雙邊。含舊單最大gross分別31.88132/41.445716/63.76896，capital50/50/80均假設，loss.20/stop.05不變；退出reserve最大.11889001/.12455701/.13778001，各低於無既有虧損的.20。較早把minimum base誤設.00001的探索結論已作廢。未改example/local size或資金，非canary授權／margin proof／fee-neutral候選晉級。 |

## 2026-09-06 本機快速驗收（使用者最新指示）

VPS暫緩，本輪不再連線／部署或等待其授權；日常驗收改為5分鐘本機dry＋受影響測試。先前ignored `vps_review/mm-v2-dry-review.zip`（SHA256 `1a90aae267e036db78e7df3901711aaec08b613ed660c7e2495757dddb809612`）未上傳，來源已落後本輪改碼；只保留歷史準備紀錄。

Windows clock：原W32Time雖Running／Automatic，SpecialPollInterval32768s；01:22獨立NTP顯示本機慢約58–72ms。依先前已授權的啟動／自動啟動／校時範圍，01:24備份至TEMP `mm_v2_w32time_backup_20260906_012418.json`，設MinPollInterval=MaxPollInterval=6、SpecialPollInterval=64、UpdateInterval=100、FrequencyCorrectRate=2、Cloudflare client peer，先依一致樣本有界校正+0.0601055s，再restart/resync。設定參考[Microsoft高精度時間指南](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/configuring-systems-for-high-accuracy)，不保證網際網路絕對精度。LastSuccessfulSync連續01:24:23→01:25:27→01:39:35；01:39獨立NTP偏差0.33／4.59／0.68ms，證明本輪持續自動poll。修後strict三次aligned唯讀preflight PASS、receipt age0.309–0.333s、pre/post authenticated0/0、disconnect；REST7200／WS22，不代表live配額。

Dry新增明確CLI `--allow-delayed-dry-book`：source age−100..10000ms，預設strict0..3000ms。Live無論是否帶bounded-flatten授權皆在settings／connect前拒絕此例外；shared adapter在active live admission時亦拒絕。Receipt3s／來源單調／nonce/offset／clock jump50ms及帳戶／訂單／reserve不變。Budget companion保存profile、accepted／outside-strict packet counts、含重驗的check counts、accepted age範圍，close後仍可讀；時間戳不改写，結果不升格economic證據。

Production刪除Phase2已被取代的empty dry cycle／port及專用exception，−156 LOC；orchestrator569→521、execution_port641→533。移除18個過時測試，保留DTO／完整inventory→exit及V1隔離契約；其餘runtime class AST未改。Analyzer以centered Decimal mean修正相同樣本平均值微幅越過max，493 LOC不變。最終**395 V2 PASS（14.272s）**；full **678（24.659s）仍為既有8F+4E**，12個方法與619測試baseline一致；stream36包含其中。18YAML leaves、size/risk及Grid production/config/tests不變，沒有新依賴或Git mutation。

Quote persistence離線比較：208組T3_01提案配對前一筆≤1s public BBO，真DryVolumeExecutionPort重現基準161 revisions、324 creates／324 cancels、mean lifetime3.9009s。只改max-age5→10s：150 revisions、604 mutations、rolling60峰值仍80、mean4.1851s；只改reprice5→10ticks：141 revisions、568 mutations（−12.3%）、峰值80→72、mean4.4503s。固定flat提案、無queue／fills、無實際API wire，不推論fee-neutral；設定未更動。

**local_quick_01 PASS：2026-09-06 01:43:20→01:48:23 Asia/Taipei，302.095s／planned300s。** Ignored `logs/mm_v2_phase7_20260906/local_quick_01.jsonl`及同名diagnostic／market／budget／analysis／postflight，config SHA256 `6ca08a103815084f705969e46892394014d9b8cdecd33c17f4ad2761bcd1dfe8`；僅將既有T3 duration1800→300，dry=true、BTC、size/risk不變。Process載入全部V2/runner/shared三檔hash，run後仍逐檔一致；最終runtime6264 LOC／282074 UTF8-LF bytes，含上述刪除與新dry接線。

100個dry quote plans、94 revisions、190 simulated creates／190 cancels、mean observed lifetime3.1494s、max6.4286s，actual orders／fills／fees／turnover為0。Final authenticated0/0、failure空；獨立新process完整REST account postflight亦0/0且disconnect。Main/launcher PIDs29540／22416均退出，process inventory無MM/Grid runner。REST／WS owned-meter與獨立診斷完全相符：rolling60峰值9900／112，account35、accountLimits34、assetDetails34、positionFunding34、trades1、orderBooks3，WStext515/ping10/close1；未含native signer startup請求，不代表有成交時的配額證明。

Profile明示delayed_dry；2618個accepted packets全部落在strict範圍，age14.8933..201.0266ms，outside-strict packets／checks均0。本轮證明短dry可運行，沒有實際觸發放寬範圍；邊界由離線測試驗證。Max event-loop lag27.22ms、max telemetry emit2.983ms，無>0.1s樣本；四次外部主Python樣本working set141.29–142.80MB、private126.44–127.51MB、handles533–538、threads15–19，不宣稱長時無leak。1477個public BBO保存301.239s，receipt gap max0.950s；analyzer 1s配對1290/1472=87.64%、5s1241/1453=85.41%，是public-mid診斷，無actual fills故markout／fee cover仍unavailable。Dry的ledger economics incomplete是刻意不發布實盤帳務，與獨立final account0/0分開。未執行live、VPS或新的commit/push；30分鐘strict T3未通過，但不再阻擋日常本機驗證。
