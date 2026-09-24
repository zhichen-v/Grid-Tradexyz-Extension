# Lighter 502 submission evidence

新版本在送單前將允許欄位與已簽署交易的 `tx_hash` 寫入
`logs/lighter_submission_evidence/*.jsonl` 並同步至磁碟。HTTP 錯誤保留狀態碼、
耗時及允許的 CloudFront/request trace headers；不保存私鑰、auth token、signature
或完整 signed TxInfo。這些 JSONL 不會因策略重啟或清理 `.log` 而消失，且不納入 Git。

先離線查看未確認紀錄（不載入憑證，不查網路）：

```bash
.venv/bin/python lighter_submission_diagnostics.py
```

需再次確認時，明確啟用有界唯讀查詢（依 exchange config 與 `.env`，可用
`--config` / `--env` 指定）；不必啟動策略：

```bash
.venv/bin/python lighter_submission_diagnostics.py --query --tx-hash HASH
```

Windows 使用 `.venv\Scripts\python.exe`。預設最多查詢 10 筆紀錄、每筆 3 頁歷史
（每頁 100 單），每個請求最多 5 秒；可用 `--limit`、`--pages`、`--timeout` 調整，
上限分別為 100、10、30 秒。`--journal` 可指定 JSONL 或資料夾。
預設只列未確認紀錄；指定 `--tx-hash` 可查看已確認交易的歷史證據。
GET 之間保留間隔，遇到 429 即停止本次後續查詢，不立即重試。

查詢只讀 transaction hash、active orders 與 inactive order history。環境、account/key
index 不符會跳過；訂單證據必須符合 exact client ID、market 與 owner account。
工具不下單、不撤單、不回寫 journal，也不會將歷史紀錄注入目前策略。

`tx.status` 僅為 API 原始數值；hash 查到不等於訂單已成交或已撤銷。查無資料、逾時、
分頁上限或查詢失敗都不代表「未送出／已拒絕」，不可據此直接重送。
尚未部署這項功能前的 502，若當時未留下 hash，無法事後從 journal 還原。
Lighter/CDN 內部 502 原因仍可能需要提供時間與 trace ID 請官方查詢。
