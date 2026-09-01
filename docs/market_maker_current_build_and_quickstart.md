# Market Maker 現行搭建與快速導入

> 更新：2026-09-01 Asia/Taipei（entry-reserve short-live gate）。這是「現在可用什麼、參數是什麼、如何安全導入」的入口；不可取代 [操作指南](market_maker_mvp_operating_guide.md) 的安全規則。逐輪證據見 [驗證歷史](market_maker_mvp_validation_history.md)，架構細節見 [MVP Pipeline](CODEX_MARKET_MAKER_MVP_PIPELINE.md)。

## 1. 目前狀態

| 項目 | 狀態 |
|---|---|
| 正常雙邊報價 | 已有多輪 live 證據；一般 quote 僅 `POST_ONLY` |
| Position-based ping-pong | 已驗證：持倉後只留反向 `POST_ONLY + reduce_only`，authenticated flat 後才恢復雙邊 |
| Ownership／uncertainty／reconciliation | 單一 mutation authority；未知單、uncertain mutation、reconciliation failure 一律 fail closed |
| Account audit／shutdown | Live 內建 authenticated audit；停止先撤單，結束後必須雙 postflight |
| Passive inventory unwind | Fee-aware exit、soft exit 與有界 maker-loss budget均已具備 |
| Active inventory unwind | 預設關閉；IOC expiry mapping與full／partial／residual exact-terminal path已有歷史live proof。新entry reserve、獨立loss barrier與strict cap hierarchy已有offline、dry T3及short-live prevention proof；仍未通過4h／production rollout |
| Economic stop／episode telemetry | Live達最小樣本失敗後鎖新episode、flat後`no_go`；保存bounded episode economics／execution history與WS＋ordinary reconciliation＋REST sync增量markout。最新live保留16筆maker fill，BUY／SELL各時域平均markout皆負；未達30 fills，dynamic quote controller仍停用 |
| Final 4h validation | **Short-live promotion NO-GO／4h未啟動**：2026-09-01第8個flat episode後，剩餘session unwind budget`0.067657`不足新episode完整reserve`0.075`，entry reserve於新下單前正確block；runtime0，雙postflight flat／orders0 |

最新short-live啟動前兩次authenticated preflight皆為BTC position／orders=`0/0`、used collateral=`0`、equity=`299.101926`。停止後02:52雙postflight皆reads PASS，position／orders=`0/0`、used collateral`0`、equity`299.069583`。該live只有16 completed maker fills，正式economic state仍是`collecting`；負bps與fee cover是不利訊號，但不得冒充達30-fill門檻後的正式economic NO-GO。

## 2. 搭建摘要

資料與控制流：

`trusted external BBO / position / economics` → `entry admission + risk + inventory episode` → `strategy quote` → `single coordinator` → `order manager` → `Lighter adapter` → `authenticated audit / economic stop / episode + markout metrics`

- `run_market_maker.py` 是獨立入口；核心位於 `core/services/market_maker/`，與 Grid runtime 分離。
- Coordinator 是唯一 mutation authority；每側最多一張 managed order。
- Normal quote 與 passive unwind 只用 `POST_ONLY`。唯一taker例外是明確opt-in的active lane，而且只能是反向減倉的`reduce_only LIMIT + IOC`。
- Active lane先exact撤完managed orders，再取得fresh BBO、authenticated position／audit及一次性truth generation；任何漂移都回到prepare或fail closed。
- Live在flat開新episode前先保留完整episode stop cap，且session與drawdown剩餘額都必須足夠；loss barrier每次trusted audit獨立於soft-exit latch。
- 經濟證據同時計入exact maker／taker fee、completed flat-to-flat ledger與自然flat equity，不能只看gross PnL。
- 達最小樣本後的live economic failure會鎖新episode；nonflat只收斂現有inventory，authenticated flat後停止。Episode ledger及分側markout只供量測，不會自行調窄spread。
- Stale data、unknown／foreign order、uncertain mutation、reconciliation failure、account hard stop、position／drawdown breach都會停止策略。

## 3. 兩種候選不可混稱

| Profile | 用途 | 關鍵差異 |
|---|---|---|
| 正式maker-only基線 | 日常安全基線 | `active_unwind_enabled: false`（缺省即false）、`max_session_loss_for_maker_exit: "0"`；不存在IOC lane |
| Active驗證overlay | 只供明確授權的獨立validation | `active_unwind_enabled: true`、maker-exit budget `0.05`、active after `300s`、有界IOC、episode/session=`0.075/0.10`及entry reserve |

本機`config/market_maker/test_*.yaml`全部受Git ignore保護，不能作為未來部署的唯一來源。新環境應從已提交的`config/market_maker/lighter_btc_mvp.example.yaml`複製成ignored config，再依下表填值。Example必須繼續維持active default OFF。

## 4. 現行固定參數

### 4.1 共同報價與帳戶邊界

| 參數 | 值 | 說明 |
|---|---:|---|
| Exchange／symbol | `lighter / BTC` | 專用sub-account、symbol獨占 |
| Quote mode／ping-pong | `both / true` | 持倉後抑制同向加碼 |
| Order size／max position | `0.00020 / 0.00040 BTC` | 單筆一個minimum lot；最多兩個lot |
| Half-spread／max skew | `250 / 250 ticks` | BTC tick `$0.1`時，各為`$25` |
| Reprice threshold | `125 ticks` | 搭配30秒minimum lifetime |
| Raw spread cap | `30 bps` | 超限停止可信mid報價 |
| Trend guard | `60s / 125 ticks` | 只擋逆勢、風險增加側 |
| Maker／taker fee | `0.000120 / 0.000350` | 每次live前重新authenticated核對；taker只供active overlay |
| Profit buffer | `0.5 bps` | Fee-aware退出緩衝 |
| Soft／hard position ratio | `0.50 / 0.80` | Hard zone只保留reduce-only側 |
| Margin | `1x cross` | 由preflight驗證；不是MM YAML參數 |

### 4.2 Timing、資料與hard gate

| 參數 | 值 |
|---|---:|
| Refresh／minimum lifetime | `5000 / 30000 ms` |
| Book／position stale | `3 / 10 s` |
| Position poll／order sync／health | `3 / 10 / 60 s` |
| Account audit／timeout | `15 / 15 s` |
| Max session drawdown | `0.50 USDG` |
| Max consecutive errors／cooldown | `3 / 30 s` |
| Max mutations | `8 / min` |
| Startup／unknown／shutdown | `abort / pause / cancel` |
| Required switches | `post_only:true`、`exclusive_symbol_control:true`、`require_flat_start:true` |

### 4.3 經濟與inventory lifecycle

| 參數 | Maker-only基線 | Active驗證overlay |
|---|---:|---:|
| Economic minimum | `30 completed maker fills` | 相同，不因active而豁免 |
| Completed／flat-equity門檻 | `>= +0.02 bps` | 相同 |
| Maker-exit loss budget | `0` | `0.05 USDG` |
| Soft exit | `120s / -0.5bps / reserve 0.02bps` | 相同 |
| Active enabled／after | `false / n/a` | `true / 300s` |
| Active loss trigger | n/a | `0.05 USDG` |
| Slippage／attempts／confirmation | n/a | `2 ticks / 2 / 5s` |
| Episode／session unwind cap | n/a | `0.075 / 0.10 USDG`；必須嚴格小於drawdown `0.50` |

Active IOC不是成交量工具。只有exact active order ID、反向side、amount不超過持倉、`reduce_only LIMIT + IOC`、兩階段barrier與全部loss/slippage cap同時成立，才是授權的taker事件。

## 5. 快速導入 checklist

1. 準備Python 3.12、`uv`及repo `.venv`；依 [操作指南 §2](market_maker_mvp_operating_guide.md#2-安裝credentials-與隔離) 安裝並先跑完整測試。
2. 設定既有Lighter wallet profile；credentials只能放既有安全位置，不得進YAML、命令列、log或Git。
3. 從`config/market_maker/lighter_btc_mvp.example.yaml`複製一份`test_*.yaml`；先保持`dry_run:true`及active OFF。
4. 依本頁參數表填入候選；金融值使用YAML字串。先驗loader、MM suite，再按變更風險完成T1–T3。
5. 連續做兩次read-only preflight：

   ```powershell
   .\.venv\Scripts\python.exe .\lighter_preflight.py --config .\config\exchanges\lighter_config.yaml --symbol BTC
   ```

   兩次都必須identity／wallet／reads PASS、position／orders=`0/0`、used collateral=`0`。
6. Dry T3使用source config及CLI `--dry-run`；保存code/config hash、cycles、hard counters、CPU/RSS與帳戶證據。
7. 只有新一輪明確live授權後，才建立ignored live copy；它相對source的唯一semantic diff只能是`dry_run:false`。Active overlay需額外明確授權。
8. Live使用直接PTY，不用輸出pipeline代管。停止按一次`Ctrl+C`，等待撤單、final audit與disconnect；確認runtime0後把live copy恢復`dry_run:true`，再做兩次postflight。

完整啟停、hard-stop與事故處置不可只看本頁，必須依 [操作指南 §§4–6](market_maker_mvp_operating_guide.md#4-測試與分級-dry-run)。

## 6. 最小停機卡

立即停止的最小集合：

- 未授權taker／market／self-trade，或active事件無法逐筆exact歸屬。
- Unknown／foreign order、current unresolved mutation、未對消ambiguity、reconciliation failure。
- Account／book／position失去可信度、持續WS／429、第二logical runtime或config／code／account drift。
- `abs(position) > 0.00040`、drawdown `>= 0.50`，或active的side／reduce-only／IOC／slippage／attempt／loss cap任一越界。
- `>=30` completed maker fills且authenticated natural-flat時形成明確economic NO-GO。

停止後不自動平倉。若final position非零，保持runtime0並另取明確flatten授權。

## 7. Final 4h 與最新 promotion gate

Final 4h歷史overlay source SHA-256為`73F53128787FA6B84ECCA7C147E894E81FB790F97A09AAE5EE50825809D6267E`，當時live copy為`508BB0854B151D8C16BA34E39FA7C11387242E6B89C63AE183B405F12BB959E3`。P0／P1調整後，兩份ignored overlay目前皆`dry_run:true`、episode／session=`0.075/0.10`，同SHA-256 `E60F3093E6FC2CC89875C2D79B27AC8E39C830F2FDBCC5CA1FA789573278C16D`。2026-09-01 short-live期間live copy唯一改為`dry_run:false`、SHA`937016BF...1A257`，停機後已恢復。Active base commit：`bc9e8a3e9ca8cfbd4ce215430627c4e76273c4ea`；IOC expiry修復後`lighter_rest.py` SHA-256：`419FE63D52DCD3B73EFA25AA51172DA685535B4E720AA71C670BF50AD6FEC7F3`。

| Checkpoint | Elapsed | Cycles ok/fail | Maker/taker fills | Trips | Position/orders | Net bps／cover／flat equity | Active | DD | Hard counters | Evidence |
|---|---:|---:|---:|---:|---:|---|---|---:|---|---|
| Start 21:48:54 | 0m | `6/0` | `0/0` | `0` | `0 / 2` | collecting | attempts `0` | `0` | all `0` | live log |
| 30m 22:22 | 33m | `465/0` | `9/1` | `5` | `flat / 2` | `-4.170716 / -1.91697 / -4.179724 bps` | attempts／success `1/1` | `0.065908` | listed hard-stop counters `0`; 3 transient reads recovered | `market_maker_final_4h_30m_20260830-2222.log` |
| Session-cap stop 22:49:54 | 61m05s | `832/0` | `18/2` unique | `9` | `long 0.00020 / 0 authenticated` | `-3.404715 / -1.33947 / unavailable` | attempts／success／partial／block `3/3/1/1` | `0.094644 / max 0.102764` | ambiguity／unresolved／reconcile／unknown／429／WS `0`; 3 transient reads recovered | `market_maker_final4h_session_loss_cap_stop_20260830-224954.log` |
| Entry-reserve block 02:47:25／final 02:47:56 | 23m14s | `315/0` | `16/0` | `8` | `flat / 0 postflight` | `-1.280784 / -0.067320 / -1.280785 bps` | attempts `0`; entry block `1` | `0.032343 / max 0.035207` | failed／ambiguity／unresolved／reconcile／unknown／429 `0`; read `1`與WS `1`均恢復 | `market_maker_short_live_entry_reserve_nogo_20260901-024756.log` |

IOC expiry修復後，首個active IOC完整成交；另一episode又以partial IOC加residual IOC取得exact terminal proof。因此active execution本身已live證明。22:49:54第10輪long達active deadline時，active decision明確判定新的marketable unwind會超過`0.10` session cap；該decision沒有保留一個可獨立引用的精確active projection值，runtime在送出第4次active order前以`active_unwind_blocks=1`拒絕並fatal stop。這不是ambiguous submission或drawdown breach。Final evidence SHA-256：`FEC3343ED3D703482BC9F0DDFA46DFD0A1A6369B545093D31BC66E8E8E7BBF20`；metrics SHA-256：`C56F9F436C2D4F0E00A6AA22A57998BAE3BECC4BB4E2A4568577919F413F4EC8`。30m evidence／metrics SHA分別為`29938A61339B2F0EDF08D3764292F161E4BAC26D7316579594DF8539A35F7E12`／`0A1851250BBE031C833EB3C473C21AC1FC8456A3C3954D69339C752703D1CEC6`。Final raw evidence是當日累積`market_maker.log`副本；本輪scope為21:48:54起（保存檔第576–943行），前段舊事故不得併入本輪counter。

最新short-live停止後runtime0，live copy已恢復`dry_run:true`且與source同SHA。02:52兩次authenticated postflight皆identity／wallet／reads PASS，position／open orders=`0/0`、used collateral`0`、equity`299.069583`。本場16 completed maker fills／8 authenticated-flat episodes，P0 entry reserve於session餘額不足完整stop時block新episode；因此promotion NO-GO且4h未啟動。Raw evidence SHA`BC60B6C4...6F1109`，metrics SHA`057C868C...F81F79`。

## 8. 已知限制

- Active IOC exact-terminal path已有歷史live proof；新entry reserve亦已有short-live prevention proof，但目前只證明「付不起完整stop時不開新episode」，未證明4h穩定或production readiness。不得為完成run而放寬cap。
- Markout／episode telemetry已取得16筆live maker fill；BUY／SELL的1／5／15／60秒平均值全負。Side-specific widening／pause controller仍停用，須先離線形成單變因候選並重新驗證。
- 最新場只運行23分14秒、16 completed fills；正式30-fill economics仍未完成，且short-live promotion已NO-GO，所以4h沒有啟動。
- 最新02:52 authenticated truth為flat／orders0；系統仍不會在未授權時自動flatten，任何後續啟動前都需fresh reads。
- 本地ignored候選不是portable deployment artifact；導入必須從committed example重建並重新做fresh preflight、測試與fingerprint。
- VPS部署、跨symbol、多帳戶與自動flatten均不在目前完成範圍。

## 9. 文件索引

- [Market Maker MVP 操作指南](market_maker_mvp_operating_guide.md)：唯一安全與啟停來源。
- [Market Maker 現況與優化方向](market_maker_status_and_optimization.md)：設計判定、經濟與下一步。
- [Market Maker MVP 驗證歷史](market_maker_mvp_validation_history.md)：逐輪事故、evidence與SHA。
- [Market Maker MVP Pipeline](CODEX_MARKET_MAKER_MVP_PIPELINE.md)：程式結構與資料流。
