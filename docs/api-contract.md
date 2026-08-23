# 风控 API 契约

服务默认监听 `127.0.0.1:8765`：

```bash
python3 -m src.risk_api --config config/ftmo-v2.json
```

生产环境使用两类凭证：

```text
X-Risk-Token: <administrator-token>
X-Account-Credential: <one-account-secret>
```

- 管理员令牌：日历同步、健康检查、Prometheus、资格汇总和凭证管理。
- 账户凭证：绑定一个 `account_id` 和作用域，用于账户同步、交易评估、执行回报、守护状态和资格历史。
- 管理员令牌默认不能调用带 `account_id` 的 `/v1/evaluate`，防止平台误配共享高权限令牌。
- Prometheus 可以使用 `Authorization: Bearer <administrator-token>`。

## `GET /health`

响应：

```json
{
  "ok": true,
  "service": "ftmo-risk-api",
  "rule_version": "ftmo-v3-2026-08-23",
  "news_data_age_seconds": 0,
  "market_data_age_seconds": 0,
  "persistent_state": true,
  "account_credentials_required": true,
  "mtls_enabled": false
}
```

新闻和休市字段还包含最近持久化时间、年龄和是否已经从 SQLite 恢复。

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

同步成功后，服务先把快照、内容哈希、`fetched_at` 和 `rule_version` 原子写入 SQLite，再替换内存日历。重启时自动恢复最近快照。生产评估只使用服务端日历，客户端不能覆盖事件列表或新鲜度。事件 ID 必须唯一；相同 `fetched_at` 只能重放相同内容，不能用冲突内容覆盖。

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
  "rule_version": "ftmo-v3-2026-08-23",
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

## 账户凭证管理

以下接口只接受管理员令牌：

```text
POST /v1/admin/accounts/{account_id}/credentials
POST /v1/admin/credentials/{credential_id}/rotate
POST /v1/admin/credentials/{credential_id}/revoke
GET  /v1/admin/credentials?account_id={account_id}
```

创建请求可以指定 `not_before`、`expires_at` 和 `scopes`；省略时使用配置中的默认 TTL 和账户标准作用域。创建和轮换响应中的 `secret` 只返回一次，数据库只保存随机盐和摘要。

```json
{
  "expires_at": "2026-09-22T12:00:00+00:00",
  "scopes": [
    "account:sync",
    "trade:evaluate",
    "trade:execution",
    "calendar:read",
    "qualification:read",
    "qualification:write"
  ]
}
```

轮换可设置 `overlap_seconds`，旧凭证在重叠窗口结束后自动失效；设为 `0` 立即失效。凭证同时校验 `account_id`、作用域、`not_before`、`expires_at` 和 `revoked_at`，不能跨账户使用。列表接口从不返回明文秘密或摘要。

## 资格历史同步

资格统计按 `account_id + phase + cycle_id` 隔离。`phase` 支持 `evaluation`、`verification` 和 `ftmo_account`；1-Step 不允许 `verification`。切换 Verification 或新 Reward 周期时必须使用新的 `cycle_id`，避免把前一阶段利润带入当前资格。

### `POST /v1/closed-trade-sync`

同步包含佣金、Swap 和其他费用后的已平仓净损益：

```json
{
  "account_id": "mt5-10001",
  "trade_id": "deal-12345",
  "phase": "evaluation",
  "cycle_id": "challenge-2026-08",
  "closed_at": "2026-08-22T14:00:00+00:00",
  "net_profit": "2500.00",
  "symbol": "EURUSD",
  "source": "mt5-history"
}
```

`trade_id` 在账户内幂等；相同 ID 的冲突内容会被拒绝。Best Day 和 Profit Target 使用当前阶段、当前周期的已平仓净损益。

### `POST /v1/trading-day-sync`

每次发现新开仓时同步开仓时间：

```json
{
  "account_id": "mt5-10001",
  "phase": "evaluation",
  "cycle_id": "challenge-2026-08",
  "opened_at": "2026-08-22T09:00:00+00:00",
  "source": "mt5-history"
}
```

服务按 `Europe/Prague` 将开仓事件归入 FTMO 日。同一天开多个仓位只计一个 Trading Day；持仓跨日不会增加天数。

### `POST /v1/qualification-history-sync`

已平仓交易和开仓日全部同步后提交完整性水位：

```json
{
  "account_id": "mt5-10001",
  "phase": "evaluation",
  "cycle_id": "challenge-2026-08",
  "history_start_at": "2026-08-01T00:00:00+02:00",
  "complete_through": "2026-08-23T12:00:00+00:00",
  "source": "mt5-history"
}
```

`complete_through` 不能倒退，且必须覆盖最新账户快照。没有完整性水位、阶段不匹配、历史未覆盖最新快照或账户结算基线不确定时，看板返回 `data_uncertain=true`、`eligible=false`。

## 资格看板

```text
GET /v1/qualification?account_id={account_id}
GET /v1/qualification/accounts
GET /dashboard/qualification
```

单账户接口接受账户凭证；账户汇总接口接受管理员令牌。浏览器页面本身不包含数据，连接后用 Bearer 管理员令牌读取汇总。

响应分别返回：

- `profit_target`：目标金额、当前净利润、完成百分比和是否达标；
- `minimum_trading_days`：Prague 开仓日列表、已完成天数和要求天数；
- `best_day_rule`：最盈利日、Positive Days' Profit、比率、上限和是否符合；
- `history`：阶段、周期、起始时间、完整性水位和来源；
- `qualification_status`：`eligible`、`in_progress`、`uncertain` 或 `not_applicable`。

资格接口不参与 `/v1/evaluate` 的单笔交易许可。`ALLOW` 与资格达标是两个独立结论。

## `GET /metrics`

返回 Prometheus 文本格式，接受管理员令牌或 Bearer 管理员令牌。指标包括：

- HTTP 请求和状态码；
- 风控决定代码；
- 新闻/休市日历是否存在、年龄、同步和恢复结果；
- `GREEN/AMBER/RED/LOCKED/BREACH` 账户数量；
- 未知执行结果数量；
- SQLite 健康状态；
- 备份/恢复结果和最后备份年龄。

告警规则见 `monitoring/alerts.yml`。


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
  "rule_version": "ftmo-v3-2026-08-23",
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
