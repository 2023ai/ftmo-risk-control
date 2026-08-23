# 风控 API 契约

服务默认监听 `127.0.0.1:8765`：

```bash
python3 -m src.risk_api --config config/ftmo-v2.json
```

生产环境必须使用随机的 `RISK_API_TOKEN`，平台端在请求头发送：

```text
X-Risk-Token: <same-token>
```

## `GET /health`

响应：

```json
{
  "ok": true,
  "service": "ftmo-risk-api",
  "rule_version": "ftmo-v2-2026-08-23",
  "news_data_age_seconds": 0,
  "market_data_age_seconds": 0,
  "persistent_state": true
}
```

## `POST /v1/news-sync`

新闻同步器将已经映射到交易品种的事件推送到本地服务。服务按 `fetched_at` 计算数据年龄；Standard FTMO Account 的新增风险请求在新闻数据缺失或超过 60 秒时会被拒绝。

```json
{
  "fetched_at": "2026-08-22T12:00:00+00:00",
  "events": [
    {
      "event_id": "NFP-2026-08-22",
      "release_time": "2026-08-22T12:30:00+00:00",
      "affected_symbols": ["EURUSD", "XAUUSD"],
      "importance": "high",
      "source": "company-news-mapper"
    }
  ]
}
```

同步成功后，`/v1/evaluate` 可以省略 `news_events`，服务会使用最近一次缓存。生产评估只使用服务端缓存，客户端不能覆盖事件列表或新鲜度。事件 ID 必须唯一；相同 `fetched_at` 只能重放相同内容，不能用冲突内容覆盖缓存。

## `POST /v1/market-sync`

同步持续至少 2 小时的周末和品种休市时段：

```json
{
  "fetched_at": "2026-08-22T12:00:00+00:00",
  "closures": [
    {
      "closure_id": "EURUSD-weekend",
      "start_time": "2026-08-28T21:55:00+00:00",
      "end_time": "2026-08-30T21:05:00+00:00",
      "affected_symbols": ["EURUSD"],
      "source": "approved-broker-symbol-schedule"
    }
  ]
}
```

同步脚本会在上传时把 `fetched_at` 替换成当前 UTC 时间。未来超过 30 秒的时间戳会被拒绝。休市 ID 必须唯一；相同 `fetched_at` 的冲突内容也会被拒绝。

## `POST /v1/market-status`

平台守护进程和 `/v1/news-status` 一起查询：

```json
{
  "account_id": "mt5-10001",
  "symbol": "EURUSD",
  "now": "2026-08-28T21:50:00+00:00"
}
```

`now` 必须与风控服务器 UTC 时间相差不超过 30 秒；超出范围会返回 `400`。实际休市窗口使用风控服务器接收时间判定，客户端时间不能移动规则窗口。

所有阶段在长休市前 120 分钟得到 `open_blocked=true`。Standard FTMO Account 在前 10 分钟额外得到 `force_flat=true` 和 `cancel_pending=true`。

## `POST /v1/news-status`

平台守护进程至少每 5 秒按持仓和挂单品种查询一次：

```json
{
  "account_id": "mt5-10001",
  "symbol": "EURUSD",
  "now": "2026-08-22T12:21:00+00:00"
}
```

响应：

```json
{
  "ok": true,
  "symbol": "EURUSD",
  "news_data_stale": false,
  "open_blocked": true,
  "force_flat": true,
  "cancel_pending": true,
  "hard_window": false,
  "emergency_alert": false,
  "event_ids": ["NFP-2026-08-22"]
}
```

`now` 必须与风控服务器 UTC 时间相差不超过 30 秒。实际新闻窗口使用风控服务器接收时间判定；平台机器仍应启用可靠的系统时钟同步。

- `T-10` 到 `T-2`：`force_flat=true`、`cancel_pending=true`。
- `T-2` 到 `T+2`：`hard_window=true`，守护程序不得再主动发送开仓、平仓或修改指令；若仍有持仓则触发紧急告警。
- 新闻数据缺失或超过 60 秒：`open_blocked=true` 和 `emergency_alert=true`。

## `POST /v1/account-sync`

平台每次评估前同步最新账户状态。第一次同步必须提供经过核对的 `day_start_balance` 和 `highest_settled_balance`；之后由 SQLite 状态层按 Prague 日界线维护。

```json
{
  "account_id": "mt5-10001",
  "account_type": "two_step",
  "phase": "evaluation",
  "style": "standard",
  "initial_capital": "100000",
  "day_start_balance": "100000",
  "highest_settled_balance": "100000",
  "balance": "100000",
  "equity": "99800",
  "current_open_risk": "100",
  "as_of": "2026-08-22T12:00:00+00:00"
}
```

使用 `account_id` 的 `/v1/evaluate` 请求不再上传账户规则、快照或频率；服务读取已同步账户和 SQLite 频率状态。

`as_of` 必须与服务器时间相差不超过 30 秒，用于检查平台时钟和计算快照年龄。Prague 日界线切换以 API 服务器接收请求的时间为准；客户端不能用偏移时间提前或延后重置日亏基线。`current_open_risk` 对已有持仓按当前可平仓报价到止损的剩余权益风险计算，对挂单按目标入场价到止损计算。

响应快照中的 `day_locked` 由服务维护，触发后持续到下一 FTMO 日；`breach_latched` 在观察到官方亏损底线后持续保留，不接受客户端覆盖。平台同步请求不应发送这两个字段。

## `POST /v1/settlement-sync`

日界线同步器在已核对 FTMO 日结算余额后调用：

```json
{
  "account_id": "mt5-10001",
  "ftmo_day": "2026-08-23",
  "settled_balance": "104000",
  "settled_at": "2026-08-23T00:00:00+02:00",
  "source": "approved-platform-settlement"
}
```

如果账户跨过 Prague 日界线时没有已确认的结算记录，服务会使用最后一次余额作为临时估计，同时将账户标记为 `data_uncertain=true`，禁止新增风险；平仓、减仓和取消挂单仍可执行。补交确认后才恢复新增风险。

同一账户和 FTMO 日的已确认结算记录不能被更旧时间戳覆盖；相同时间戳但余额或来源不同也会被拒绝。正式确认记录可以覆盖系统在缺失结算时生成的未确认推断记录。

## `POST /v1/evaluate`

请求：

```json
{
  "account_type": "two_step",
  "phase": "ftmo_account",
  "style": "standard",
  "snapshot": {
    "initial_capital": "100000",
    "day_start_balance": "100000",
    "highest_settled_balance": "100000",
    "balance": "100000",
    "equity": "99800",
    "as_of": "2026-08-22T12:00:00+00:00",
    "current_open_risk": "100",
    "data_age_seconds": 0
  },
  "request": {
    "symbol": "EURUSD",
    "action": "open",
    "requested_at": "2026-08-22T12:00:00+00:00",
    "volume": "1.00",
    "entry_price": "1.1000",
    "stop_loss": "1.0800",
    "loss_per_volume_unit": "100",
    "estimated_costs": "8",
    "additional_risk": "0",
    "is_risk_increasing": true,
    "idea_id": "strategy-a-20260822-001"
  },
  "frequency": {
    "open_times": [],
    "request_times": [],
    "last_modify_by_symbol": {}
  },
  "news_data_age_seconds": 0,
  "market_data_age_seconds": 0,
  "news_events": [
    {
      "event_id": "NFP-2026-08-22",
      "release_time": "2026-08-22T12:01:00+00:00",
      "affected_symbols": ["EURUSD", "XAUUSD"],
      "importance": "high",
      "source": "ftmo-calendar"
    }
  ],
  "market_closures": []
}
```

风险增加型 `modify` 请求必须提供 `additional_risk`，表示修改后相对当前持仓新增的止损货币风险；风控会把它与当前剩余预算比较。`open` 的 `is_risk_increasing` 固定为 `true`，`close`/`cancel` 固定为 `false`，不能由客户端用相反值绕过全局闸门。

响应重点：

```json
{
  "ok": true,
  "rule_version": "ftmo-v2-2026-08-23",
  "decision": {
    "code": "REJECT_NEWS",
    "allowed": false,
    "reasons": ["NFP-2026-08-22 is inside the FTMO hard news window"],
    "risk_budget": null
  },
  "account": {
    "status": "GREEN",
    "daily_loss": "200",
    "daily_loss_limit": "95000",
    "max_loss_limit": "90000",
    "internal_daily_stop_limit": "96000",
    "internal_max_loss_stop_limit": "92000",
    "daily_utilization": "0.04",
    "max_loss_utilization": "0.02"
  }
}
```

平台端只有在 `decision.allowed == true` 时才提交新增风险。`REJECT_*` 必须记录 `code` 和 `reasons`。带 `account_id` 的生产评估使用风控服务器接收时间执行新闻、休市和频率判断；客户端 `requested_at` 只用于时钟健康检查和请求身份。

当日服务器请求达到 `warning_requests_day` 时，允许的决定会在 `reasons` 中返回预警；达到 `stop_requests_day` 后返回 `REJECT_FREQUENCY`。计算新交易预算时，现有持仓和挂单的 `current_open_risk` 会同时从日亏缓冲、最大亏损缓冲和总开放风险上限中扣除。

每个 POST 响应包含 `request_id`。平台端应把这个 ID 写入本地日志，并在发生平台订单结果未知、超时或人工复核时使用它关联风控决定。带 `account_id` 的评估必须使用持久化状态；无状态 `/v1/evaluate` 默认关闭，仅可由测试/回放服务器显式开启。

## `POST /v1/execution-result`

平台执行后回传结果。明确失败的开仓会释放开仓频率名额，明确失败的修改会释放修改冷却预留；超时、仅确认已受理或结果未知时使用 `outcome: "unknown"`，保留预留并人工复核。相同的评估或执行 `request_id` 重试会返回第一次保存的结果；同一 ID 复用不同内容会被拒绝。

```json
{
  "account_id": "mt5-10001",
  "request_id": "mt5-10001-1724328000-15",
  "outcome": "success",
  "action": "open",
  "symbol": "EURUSD",
  "occurred_at": "2026-08-22T12:00:01+00:00",
  "platform_status": "TRADE_RETCODE_DONE",
  "platform_order_id": "123456789"
}
```

旧客户端仍可发送 `success: true/false`；新客户端应使用 `outcome: "success" | "failure" | "unknown"`。

`occurred_at` 可以保留平台实际执行时间，便于断线后补报，但不能比服务器时间提前超过 30 秒。执行活动和保留释放使用服务器接收时间写入，客户端时间不能移动频率保留或清理窗口。


## `POST /v1/position-size`

该接口使用客户端提供的无状态快照，生产服务默认禁用，只用于显式开启的测试或回放实例。生产交易量必须由持久化账户状态下的 `/v1/evaluate` 最终确认，不能把本接口响应直接当作下单许可。

请求字段：

```json
{
  "account_type": "two_step",
  "phase": "evaluation",
  "style": "standard",
  "snapshot": {
    "initial_capital": "100000",
    "day_start_balance": "100000",
    "highest_settled_balance": "100000",
    "balance": "100000",
    "equity": "100000",
    "as_of": "2026-08-22T12:00:00+00:00",
    "current_open_risk": "0",
    "data_age_seconds": 0
  },
  "loss_per_volume_unit": "100",
  "volume_step": "0.01",
  "min_volume": "0.01",
  "max_volume": "100"
}
```

响应：

```json
{
  "ok": true,
  "rule_version": "ftmo-v2-2026-08-23",
  "position_size": {
    "volume": "2.50",
    "expected_loss": "250.00",
    "risk_budget": "250"
  },
  "account_status": "GREEN"
}
```

## 时间和精度

- 所有时间必须包含时区偏移；推荐使用 UTC `Z`。
- 金额和数量使用字符串传输，避免浮点误差。
- `loss_per_volume_unit` 必须已经由 MT5/cTrader 按平台合约规格、报价货币、佣金和滑点换算完成。
- 新闻事件的 `affected_symbols` 必须由公司新闻映射表生成，不应只根据货币代码猜测。
- 所有新增风险请求必须有不超过 60 秒的新鲜新闻数据和不超过 1 小时的市场休市日历。
- 没有日历或日历过期时，服务返回 `REJECT_DATA_STALE`。
