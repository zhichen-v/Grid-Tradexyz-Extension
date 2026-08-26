# Market Maker MVP 驗證歷史索引

本檔保存 `market_maker_mvp_operating_guide.md` 的歷次本地驗證索引，讓主操作指南只保留目前可執行規則與最新候選。表內帳戶、餘額、持倉、訂單、fee、config SHA與測試數字都是**當時快照**，不得作為目前狀態或下一輪 preflight 的替代品。

原 `12.1–12.19` 的完整逐輪原文可由已push的commit讀取：

```powershell
git show 7c04fd224fe2eceb3dc1f42051c751eac5a748c1:docs/market_maker_mvp_operating_guide.md
```

目前判定、現行候選與下一步只看主指南第 12 節及 fresh authenticated reads。

## 稽核索引

| 原節 | 日期／階段 | 判定 | 保留重點 |
|---|---|---|---|
| 12.1 | 2026-08-20 初版修復 | Historical completion | 新增單邊模式、raw-spread guard、shutdown／stale／uncertainty fail-closed、WS lifecycle及position sign修復；當時離線測試數字不是現行baseline。 |
| 12.2 | 第一次Step B live | **NO-GO** | WS position `sign=-1`被錯讀，reduce-only bid約6秒後被錯誤safety cancel；無fill，postflight orders 0。 |
| 12.3 | 修復後Step B dry-run | **Dry safety GO** | `bid_only`約1824秒、`361/361` cycles；position方向與exposure正確、真實mutation 0；當時仍保留既有short，快照不可當現在。 |
| 12.4 | Step C歷史baseline | Historical baseline | `4000 ticks / raw guard 100 bps / 2 mutations/min`等保守值；authenticated歷史交叉驗證maker/taker fee約`0.000120/0.000350`。 |
| 12.5 | 未完成清單 | Superseded checklist | 記錄當時Step D/E、live proof與VPS缺口；後續結果由12.10–12.19及現行指南取代。 |
| 12.6 | 恢復順序 | Superseded procedure | 記錄當時Step D重新授權、fresh pre/postflight、監控與一次一參數規則；通用安全規則已整理回主指南。 |
| 12.7 | 2026-08-20 flat恢復 | **Dry safety GO / live eligibility NO-GO** | Flat exact-config dry-run約1805秒無mutation；raw spread高於當時25bps gate，因此未live。 |
| 12.8 | Spread配對校準、第二次Step B | **NO-GO** | `4000 ticks / raw guard 100 bps` dry smoke通過；live bid後因錯誤PTY pipeline未走正常shutdown且該單其後成交，留下long 0.00020。 |
| 12.9 | 殘倉處置、Step B retry、Step C | **Step B GO / Step C GO** | 授權後一次reduce-only IOC平倉；direct-PTY Step B完成create/cancel/flat proof；Step C雙邊極小額驗證完成。 |
| 12.10 | 高換手候選 | **Dry GO / first live NO-GO** | 候選改為`500 ticks / reprice 250 / raw 30 / lifetime 30s / 8 mutations/min`；首次live出現fast-fill uncertainty。 |
| 12.11 | 2026-08-23 fast-fill修復 | **T3 GO** | MM latch/reconciliation與paused status freshness修復；切到隔離測試sub-account；exact-config 30m dry-run通過。 |
| 12.12 | Step D首次高換手live | **NO-GO / stopped early** | 約5m15s；4 fills、short到cap；1次ambiguous cancellation及risk-reduction mutation阻擋。依當輪授權一次reduce-only IOC後flat。 |
| 12.13 | Step D事故修復 | **Offline + T3 GO** | 修復stale-active cancellation read與risk-reduction emergency create優先權；deterministic tests及30m dry-run通過。 |
| 12.14 | Step D live retry | **NO-GO / stopped early** | 約28m55s；position自然回flat且risk side正常，但2次cancel terminal visibility超出`2 / 0.25s`窗口；postflight flat。 |
| 12.15 | Cancellation visibility修復 | **Offline + T3 GO** | Cancellation-only read window改為`4 / 0.5s`且不重送signer mutation；deterministic tests與30m dry-run通過；獨立monitor DNS fault有bounded紀錄。 |
| 12.16 | Step E E0/E1 | **T3 GO；首次live safety NO-GO / economic INCONCLUSIVE** | Fee truth調為maker`0.000120`，half-spread縮至250ticks；live monitor跨endpoint分類及stop propagation有缺口，2筆maker只完成開倉，留下short 0.00040。 |
| 12.17 | Long-run前候選 | **Offline candidate** | 將account audit與hard-stop propagation內建；建立authenticated trade ledger、fee/net/equity gate與嚴格`1x cross`／ownership驗證。 |
| 12.18 | 最後候選T3 | **Software/safety GO** | 修復integrator-fee `null` capability邊界；約1872秒、`360/360` cycles，audit healthy、真實mutation 0；不代表economic GO。 |
| 12.19 | 4小時live long-run | **Runtime/safety GO / economic INCONCLUSIVE / volume NOT MET** | 4h穩定、3 maker fills、turnover`46.993860`、最後short 0.00020；後續證明舊MM取`levels[0]`造成假性寬BBO，因此其spread／eligible／流動性解讀已失效，但成交、cleanup與資源數據仍是歷史證據。 |
| 12.20 | MM-only BBO 正規化與 T3 | **Software/data safety GO** | MM boundary 以有效價量重建真正 BBO；30m T3 `354/354` cycles、eligible `99.84%`，未修改 shared Lighter/Grid。 |
| 12.21 | BBO 修復後 E1 canary | **Volume improved / economic NO-GO** | 約224秒、8 maker fills、turnover `126.892880`；completed net `+0.0230789 bps` 低於當時 `+0.10 bps` 門檻。 |
| 12.22 | Fee-aware exit floor | **Observed-path positive / formal inconclusive** | 20 maker fills、turnover `308.517560`；partial residual 使 formal completed gate 停在6，促成 exact-residual／oversized reduce-only 後續工作。 |
| 12.23 | Exact-residual live | **NO-GO / reverted** | Lighter 以 `21706 invalid order base or quote amount` 拒絕 sub-min reduce-only；策略依 uncertainty hard-stop，未重送。 |
| 12.24 | Oversized reduce-only 修復 | **Offline/regression GO** | 小殘倉仍提交 exchange minimum、只允許成交到 flat；final audit 可觀察。人工授權 cleanup 證實 exchange semantics。 |
| 12.25 | Oversized 修復後短 E1 | **Safety GO / economic inconclusive** | 原250-tick邊界約916秒，2 maker sells 到 short cap；只有 reduce-only buy，未自然 flat，停止後 orders 0。 |
| E2a | 250-tick 快速樣本 | **Positive signal / below formal sample gate** | 14 completed maker fills、約344秒、turnover `187.833580`、net `+0.0078799704`、`+0.4195187250 bps`、cover `1.3495989375`、DD `0.030884`。 |
| E2b | 250-tick 延伸樣本 | **Positive signal / terminal proof complete** | turnover `344.023420`、net `+0.0143371896`、`+0.4167503945 bps`、cover `1.3472919954`；exact terminal proof `unresolved=0`。 |
| E2c | 200-tick canary | **NO-GO / rollback 250** | 29 completed、turnover `509.555250`、net `+0.0015133700`、`+0.0296998216 bps`、cover `1.0247498513`、DD `0.048974`、約80–100 fills/h；經濟餘裕過薄，且舊 `3×0.5s` read retry 在 CloudFront 502 burst 後 fail-closed。 |
| E2d | Account-read retry與soft-exit信任修復 | **Offline/regression GO；live path observed** | 讀取改為 `5×1s`、外層 audit timeout 仍為10秒；soft-exit 必須等待最新 processed fill 之後的新 authenticated audit，startup retry 重新建立 baseline。後續live各吸收1次瞬時read failure，未形成consecutive failure；加入降低庫存側create優先測試後完整MM為`278/278`，full repo `515`維持既知Grid baseline `8F+4E`。 |
| E2e | 200-canary受控cleanup | **Recovery／terminal proof GO** | 只掛 `POST_ONLY + reduce_only BUY 0.00040 @ 78529.5`，authenticated maker/tick120成交，fee `0.003769416`；兩次postflight皆flat／orders0／unresolved0，collateral `299.555125`。 |
| E2f | 250-tick正式經濟場次（方向優先修復前） | **Safety／economic GO；後續由E2g取代fingerprint** | 約1195秒，32 completed／12round trips，turnover `596.629560`、exact fee `0.07159554720`、net `+0.00160445280`、`+0.0268919428 bps`、cover `1.02240995`、約 `1800 USDG/h`／`96.5 fills/h`、DD `0.033816`；一次exact-reconciled cancellation race及一次瞬時read failure，terminal unresolved0。此場暴露mutation緊張時固定BUY-first的方向問題。 |
| E2g | 250-tick／8 mutations + 降低庫存側優先 | **Previous-fingerprint short-session safety／economic GO** | 約1759秒，38 completed／15round trips，turnover `690.898688`、exact fee `0.08290784256`、net `+0.00206015744`、`+0.0298185172 bps`、cover `1.02484876`、flat-equity `+0.0298162384 bps`、約 `1416 USDG/h`／`77.9 fills/h`、DD `0.031468`；failed／unknown／unresolved／reconciliation failure皆0，一次read failure由bounded retry吸收，雙postflight乾淨。 |
| E2h | 250-tick／10 mutations單變因canary與操作者停止 | **吞吐比較INCONCLUSIVE；完整場次economic NO-GO；reverted to 8** | 約650秒；停止前22 completed／10round trips，completed turnover `345.085000`、net `+0.00126980000`、`+0.0367967312 bps`、cover `1.03066394`；runtime wall-rate（含第23筆open authenticated fill）約 `2005 USDG/h`／`127.8 fills/h`，DD `0.015979`。一筆cancel曾缺terminal proof，兩個cycle fail-closed後exact sync解除，無duplicate／unknown／reconciliation failure。操作者在30筆前要求停止，停止窗口留下short `0.00020`；maker reduce-only `78517.7`回補後，authenticated trade-ledger全場turnover `376.491440`、net `-0.00313897280`、`-0.0833743471 bps`、cover `0.93052138`；baseline／final equity `299.558978 / 299.555839`，與ledger只差`2.72e-8`。雙postflight為0/0/0；10尚無promotion證據，不能宣稱已證明10較差。 |
| E2i | REST fill-generation邊界審查與暫停交接 | **P0 deferred；未納入checkpoint** | `position_refresh_required` 同時包含純cancel與visible fill：全套用會造成soft／hard reprice ping-pong，只在full sync套用又會漏掉reconcile-confirmed fill。匆忙實驗已撤回；下一輪須拆出精確 `fill_observed`並補同淨倉位fills／純reprice反例後才能進long-run。帳戶維持flat／orders0／unresolved0，P1雙inventory timer亦待統一。 |

## 使用規則

- 要追原12.1–12.19的事故細節、時間線、commit、SHA、測試數或歷史postflight時，讀上列Git原文；12.20以後與E2系列以本索引、其後commit及保存的log證據為準。不要把全部歷史複製回主指南。
- 要決定能否啟動、測多久、何時停或下一個candidate時，只使用目前主指南與fresh authenticated reads。
- 歷史GO只覆蓋當時fingerprint與證據範圍；後續parser／runtime修復不會把舊事故改判，也不會自動授權live。
- VPS同步與測試在上述歷次驗證中均不屬於完成範圍。
