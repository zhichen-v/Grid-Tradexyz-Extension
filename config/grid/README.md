# Grid 設定檔說明

這份文件整理 `config/grid/` 內各 YAML 設定檔的用途，以及 `run_grid_trading.py` 目前實際支援的設定欄位。

如果範例 YAML 裡的舊註解和程式行為不一致，以這份 README 與程式實作為準。

## 設定檔清單

| 檔案 | 交易所 | 類型 | 用途 |
|------|--------|------|------|
| `example.yaml` | TradeXYZ | `long` | 完整英文範例，包含 stop loss 設定 |
| `backpack_capital_protection_long.yaml` | Backpack | `follow_long` | 做多跟隨網格，四道保護全開的完整範例 |
| `backpack_capital_protection_short.yaml` | Backpack | `follow_short` | 做空跟隨網格，四道保護全開的完整範例 |
| `tradexyz_long.yaml` | TradeXYZ | `long` | 固定區間做多範例 |
| `tradexyz_long_GOLD.yaml` | TradeXYZ | `long` | 黃金商品固定區間做多範例 |
| `tradexyz_long_NVDA.yaml` | TradeXYZ | `long` | NVDA 固定區間做多範例 |
| `tradexyz_test_follow_long.yaml` | TradeXYZ | `follow_long` | 跟隨型做多測試範例 |
| `tradexyz_test_long.yaml` | TradeXYZ | `long` | 固定區間做多測試範例 |

## 快速啟動

```bash
uv run python run_grid_trading.py config/grid/<設定檔>.yaml
uv run python run_grid_trading.py config/grid/<設定檔>.yaml --debug
```

測試後若要清空 TradeXYZ 掛單與持倉：

```bash
uv run python test_tradexyz_cancel_orders.py
```

## YAML 基本結構

所有設定都放在 `grid_system` 節點下：

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "long"
  lower_price: 187.5
  upper_price: 189.5
  grid_interval: 0.1
  order_amount: 0.1
  quantity_precision: 1
```

## 先看這幾個觀念

### 1. `grid_type` 決定整體模式

可用值：

- `long`
- `short`
- `follow_long`
- `follow_short`
- `martingale_long`
- `martingale_short`

### 2. fixed range 和 follow mode 的差別

- `long` / `short` / `martingale_*`
  需要手動提供 `lower_price` 與 `upper_price`
- `follow_long` / `follow_short`
  不需要固定價格區間，系統啟動時會用當前市價動態推算網格範圍

### 3. 實際 runtime 預設值以 `run_grid_trading.py` 為準

例如：

- `enable_notifications` 預設實際是 `false`
- `order_health_check_interval` 預設實際是 `600`
- `fee_rate` 預設實際是 `0.0001`

另外要注意：

- `GridConfig` 類別裡雖然有 `price_decimals` 欄位，但目前 `run_grid_trading.py` 不會從 YAML 讀取它
- 寫設定檔時，請優先參考這份 README 與 `run_grid_trading.py` 的載入邏輯，而不是只看 dataclass 欄位名稱

## 模式說明

### 固定區間網格：`long` / `short`

適合你已經知道希望操作的價格區間。

做多 `long`：

- `Grid 1` 是最低價 `lower_price`
- `Grid N` 是最高價 `upper_price`
- 啟動時只會掛低於市價的買單
- 買單成交後，再由反手機制掛出對應賣單

做空 `short`：

- `Grid 1` 是最高價 `upper_price`
- `Grid N` 是最低價 `lower_price`
- 啟動時只會掛高於市價的賣單
- 賣單成交後，再由反手機制掛出對應買單

網格數量是自動算出來的：

```text
grid_count = int((upper_price - lower_price) / grid_interval)
```

範例：

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "long"
  lower_price: 187.5
  upper_price: 189.5
  grid_interval: 0.1
  order_amount: 0.1
  quantity_precision: 1
```

### 跟隨網格：`follow_long` / `follow_short`

適合價格會持續漂移、你不想手動重設區間的情境。

做多 `follow_long`：

- 會以啟動時的當前價格為基準，計算上界與下界
- 只在價格向上突破網格並超過 `follow_distance` 後，才視為獲利方向脫離並重建網格

做空 `follow_short`：

- 會以啟動時的當前價格為基準，計算下界與上界
- 只在價格向下突破網格並超過 `follow_distance` 後，才視為獲利方向脫離並重建網格

跟隨網格的價格區間推算重點：

- `follow_grid_count` 決定總格數
- `grid_interval` 決定每格距離
- `price_offset_grids` 可把市價往網格內部推幾格，避免市價剛好卡在邊界

範例：

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "follow_long"
  follow_grid_count: 200
  follow_timeout: 300
  follow_distance: 2
  price_offset_grids: 5
  grid_interval: 0.5
  order_amount: 0.1
  quantity_precision: 1
```

### 馬丁網格：`martingale_long` / `martingale_short`

本質上仍是 fixed range，只是不同格子的下單量會遞增。

目前遞增量由 `martingale_increment` 控制：

- 做多時，越靠近低價區，下單量越大
- 做空時，越靠近高價區，下單量越大

範例：

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "martingale_long"
  lower_price: 180.0
  upper_price: 200.0
  grid_interval: 1.0
  order_amount: 0.1
  martingale_increment: 0.02
  quantity_precision: 1
```

## 參數總表

### 基礎欄位

| 參數 | 必填 | 實際預設值 | 說明 |
|------|------|------------|------|
| `exchange` | 是 | 無 | 交易所名稱，例如 `tradexyz`、`backpack` |
| `symbol` | 是 | 無 | 商品或交易對，例如 `NVDA`、`HYPE_USDC_PERP` |
| `grid_type` | 是 | 無 | 網格模式 |
| `grid_interval` | 是 | 無 | 每格價格距離 |
| `order_amount` | 是 | 無 | 每格的基礎下單數量 |
| `quantity_precision` | 否 | `3` | 數量小數位數 |
| `max_position` | 否 | `null` | 最大持倉限制 |
| `enable_notifications` | 否 | `false` | 是否啟用通知 |
| `order_health_check_interval` | 否 | `600` | 健康檢查間隔，單位秒 |
| `fee_rate` | 否 | `0.0001` | 手續費率，供損益估算使用 |

### fixed range 專用欄位

| 參數 | 必填 | 說明 |
|------|------|------|
| `lower_price` | 是 | 網格最低價 |
| `upper_price` | 是 | 網格最高價 |

你也可以用巢狀格式：

```yaml
grid_system:
  grid_type: "long"
  price_range:
    lower_price: 187.5
    upper_price: 189.5
```

### follow mode 專用欄位

| 參數 | 必填 | 實際預設值 | 說明 |
|------|------|------------|------|
| `follow_grid_count` | 是 | 無 | 跟隨網格總格數 |
| `follow_timeout` | 否 | `300` | 價格脫離後，等待多久才重建網格 |
| `follow_distance` | 否 | `1` | 價格超出網格邊界幾格後，才開始計時 |
| `price_offset_grids` | 否 | `0` | 啟動時把市價往網格內部偏移幾格 |

### 馬丁與反手相關欄位

| 參數 | 必填 | 實際預設值 | 說明 |
|------|------|------------|------|
| `martingale_increment` | 否 | 無 | 每格遞增量；只有大於 0 才會真正啟用馬丁邏輯 |
| `reverse_order_grid_distance` | 否 | `1` | 成交後反手單距離幾格 |

## 保護機制

這套 grid 有五種額外保護／控制模式，可以單獨開啟。

### 1. Scalping 模式

| 參數 | 實際預設值 | 說明 |
|------|------------|------|
| `scalping_enabled` | `false` | 是否啟用 |
| `scalping_trigger_percent` | `80` | 轉換成觸發格位的百分比 |
| `scalping_take_profit_grids` | `2` | 進入 scalping 後，止盈單往 break-even 外再拉幾格 |

觸發格位公式：

```text
trigger_grid = grid_count - int(grid_count * scalping_trigger_percent / 100)
```

實際行為：

- 當前價格落到 `current_grid_index <= trigger_grid` 時啟動
- 啟動後不再維持一般反手補單節奏
- 系統會集中用一張止盈單管理目前持倉
- 止盈單成交後：
  - `follow_*` 模式會重建網格
  - fixed range 模式會停止策略

例子：

- `grid_count = 200`
- `scalping_trigger_percent = 90`
- 觸發格位就是 `20`

也就是價格進入較深的風險區後，才切到 scalping 模式。

### 2. Capital Protection 模式

| 參數 | 實際預設值 | 說明 |
|------|------------|------|
| `capital_protection_enabled` | `false` | 是否啟用 |
| `capital_protection_trigger_percent` | `50` | 轉換成觸發格位的百分比 |

觸發格位公式和 scalping 相同：

```text
trigger_grid = grid_count - int(grid_count * capital_protection_trigger_percent / 100)
```

實際行為：

- 進入保護後，策略不再主動擴張風險
- 會等待權益回升到保護條件，再做 reset

例子：

- `grid_count = 200`
- `capital_protection_trigger_percent = 40`
- 觸發格位就是 `120`

### 3. Take Profit 模式

| 參數 | 實際預設值 | 說明 |
|------|------------|------|
| `take_profit_enabled` | `false` | 是否啟用 |
| `take_profit_percentage` | `0.01` | 以初始資金為基準的止盈比例 |

例子：

- `take_profit_percentage = 0.005`
- 代表策略淨利達到初始資金的 `0.5%` 時觸發

### 4. Price Lock 模式

| 參數 | 實際預設值 | 說明 |
|------|------------|------|
| `price_lock_enabled` | `false` | 是否啟用 |
| `price_lock_threshold` | 無 | 觸發鎖定的價格 |
| `price_lock_start_at_threshold` | `false` | 僅 follow mode 使用；若啟動時價格已越過閾值，是否直接以閾值當起點 |

實際行為：

- 做多：當價格 `>= threshold` 時觸發鎖定
- 做空：當價格 `<= threshold` 時觸發鎖定
- `price_lock_start_at_threshold` 只在 `follow_long` / `follow_short` 生效

### 5. Stop Loss 模式

| 參數 | 實際預設值 | 說明 |
|------|------------|------|
| `stop_loss_enabled` | `false` | 是否啟用設定價格止損 |
| `stop_loss_price` | 無 | 觸發止損的價格 |
| `stop_loss_check_interval` | `1` | 檢查間隔，單位秒 |

實際行為：

- 做多網格：當市價 `<= stop_loss_price` 時觸發
- 做空網格：當市價 `>= stop_loss_price` 時觸發
- 觸發後會取消所有掛單、市價平掉目前持倉，然後停止 grid runtime
- 若啟動時市價已經越過止損價，系統會拒絕掛出初始網格單

## 巢狀設定

### `spot_reserve`

這組設定只對現貨模式有意義，用來保留一部分底層資產，不讓策略全部用掉。

目前程式至少會用到這些欄位：

| 欄位 | 必填 | 說明 |
|------|------|------|
| `enabled` | 是 | 是否啟用 |
| `reserve_amount` | 是 | 要保留的資產數量 |
| `spot_buy_fee_rate` | 是 | 現貨買入手續費率 |

範例：

```yaml
grid_system:
  spot_reserve:
    enabled: true
    reserve_amount: 0.01
    spot_buy_fee_rate: 0.001
```

### `position_tolerance`

這組設定用於健康檢查時的持倉誤差容忍。

目前程式至少會用到：

| 欄位 | 必填 | 實際預設值 | 說明 |
|------|------|------------|------|
| `tolerance_multiplier` | 否 | `0.25` | 用 `order_amount * tolerance_multiplier` 當作持倉比對容差 |

範例：

```yaml
grid_system:
  position_tolerance:
    tolerance_multiplier: 0.5
```

## 常見範例

### 範例 1：最小 fixed range long

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "long"
  lower_price: 187.5
  upper_price: 189.5
  grid_interval: 0.1
  order_amount: 0.1
  quantity_precision: 1
  scalping_enabled: false
  capital_protection_enabled: false
  take_profit_enabled: false
  price_lock_enabled: false
  stop_loss_enabled: false
```

### 範例 2：跟隨網格加保護模式

```yaml
grid_system:
  exchange: "backpack"
  symbol: "HYPE_USDC_PERP"
  grid_type: "follow_long"
  follow_grid_count: 200
  follow_timeout: 300
  follow_distance: 2
  price_offset_grids: 5
  grid_interval: 0.04
  order_amount: 0.5
  quantity_precision: 2
  scalping_enabled: true
  scalping_trigger_percent: 15
  scalping_take_profit_grids: 2
  capital_protection_enabled: true
  capital_protection_trigger_percent: 40
  take_profit_enabled: true
  take_profit_percentage: 0.002
  price_lock_enabled: true
  price_lock_threshold: 50.0
  price_lock_start_at_threshold: false
  stop_loss_enabled: true
  stop_loss_price: 42.0
  stop_loss_check_interval: 1
  reverse_order_grid_distance: 1
  order_health_check_interval: 300
```

### 範例 3：使用巢狀 `price_range`

```yaml
grid_system:
  exchange: "tradexyz"
  symbol: "NVDA"
  grid_type: "long"
  price_range:
    lower_price: 187.5
    upper_price: 189.5
  grid_interval: 0.1
  order_amount: 0.1
  quantity_precision: 1
```

## 參數搭配建議

- `follow_*` 模式請一定設定 `follow_grid_count`
- fixed range 模式不要再放 `follow_*` 參數，避免自己混淆
- `price_lock_threshold` 通常應放在網格區間外部
- `martingale_increment` 會快速放大持倉，請先用很小數值測試
- `reverse_order_grid_distance` 調大會增加單筆目標利潤，也會降低成交速度
- scalping 與 capital protection 的百分比，本質上都是轉成格位，不是直接拿價格百分比比較
- 做多的 `stop_loss_price` 通常應放在目前價格與網格下方；做空則放在目前價格與網格上方

## 風險與注意事項

- 修改 YAML 後需要重啟程式才會生效
- stop loss 觸發後會停止策略；重新啟動前請先確認交易所掛單與持倉狀態
- 請確認 `quantity_precision` 與交易所實際最小下單單位一致
- 固定區間模式若 `upper_price - lower_price` 不能被 `grid_interval` 合理切分，實際 `grid_count` 會取整數
- 部分舊範例檔仍可能留有歷史註解；若註解和公式不同，以程式公式為準
- 實盤前請先確認交易所、商品、保護模式、清單配置都正確
