# 风控系统架构

## 1. 总体链路

```mermaid
flowchart LR
    MT5[MT5 EA] --> Adapter[平台适配层]
    CTR[cTrader cBot] --> Adapter
    Adapter --> Engine[统一风控引擎]
    Engine --> Rules[账户规则与内部阈值]
    Engine --> News[新闻日历与受影响品种]
    Engine --> Market[长休市与周末日历]
    Engine --> State[SQLite账户、日历与资格状态]
    Adapter --> Credential[账户级凭证校验]
    Engine --> Gate[执行闸门]
    Gate --> MT5
    Gate --> CTR
    Adapter --> Audit[审计日志]
    State --> Qualification[独立资格计算]
    Qualification --> Dashboard[资格看板]
    Engine --> Metrics[Prometheus指标与告警]
```

## 2. 检查顺序

每一笔开仓、平仓或修改请求按以下顺序检查：

1. 账户状态：账户是否已经触及官方日亏或最大亏损底线。
2. 内部状态：是否达到 `RED` 或 `LOCKED`。
3. 新闻状态：当前品种是否处于 FTMO 硬限制窗口或公司预防窗口。
4. 市场状态：是否接近周末或超过 2 小时的休市。
5. 交易动作：是否为开仓、平仓、修改止损、取消挂单。
6. 止损约束：开仓必须有有效止损，且止损距离符合平台最小距离。
7. 频率约束：短周期、小时、日内开仓和服务器请求是否超限。
8. 风险额度：交易的预计止损损失是否超过当前剩余风险预算。
9. 审计：记录输入快照、规则版本、决定、原因和平台返回值。

检查应默认失败关闭：风控服务不可用、新闻或市场日历过期、账户状态无法读取时，不允许新增风险。

## 3. 状态机

```text
GREEN
  | 日亏或最大亏损使用率 >= 50%
  v
AMBER
  | 使用率 >= 70%
  v
RED
  | 使用率 >= 80% 或数据异常
  v
LOCKED
  | 官方底线被触及
  v
BREACH
```

`RED`、`LOCKED` 和 `BREACH` 禁止新增风险，但仍允许不在新闻硬窗口内的减仓、平仓和撤单。AMBER 的单笔预算减半。观察到 `LOCKED` 后会在 SQLite 中保持到下一个 Prague 交易日，即使权益随后反弹也不会恢复交易；观察到官方 `BREACH` 后永久保持，必须人工复核账户状态。

## 4. 规则计算

### 4.1 日亏

```text
daily_loss = max(0, day_start_balance - current_equity)
daily_limit = day_start_balance - initial_capital * official_daily_loss_pct
daily_utilization = daily_loss / (initial_capital * official_daily_loss_pct)
```

必须同时保存：

- 当前权益；
- 当日最低权益；
- 日界线开始余额；
- 当日已平仓损益；
- 当日浮动损益；
- 佣金和隔夜利息。

### 4.2 最大亏损

2-Step 使用静态底线：

```text
max_loss_limit = initial_capital * (1 - official_max_loss_pct)
```

1-Step 使用结算余额追踪模型：

```text
max_loss_limit =
    highest_settled_balance - initial_capital * official_max_loss_pct
```

`highest_settled_balance` 只能在 FTMO 日界线结算后更新，不能使用盘中浮动权益抬高底线。

### 4.3 以损定仓

平台适配层先把入场价、止损价、品种合约规格转换为“每一单位交易量的止损货币损失”：

```text
loss_per_volume_unit =
    abs(entry - stop_loss) / tick_size * tick_value
```

实际系统应使用平台原生的利润/风险计算函数处理货币转换、合约大小、佣金和点值。

已有持仓必须按当前可平仓报价到止损的剩余权益风险计算，不能继续使用入场价；否则盈利仓位回撤到止损时会低估权益下降。挂单尚未进入权益，因此仍按目标入场价到止损计算。现有开放风险会先从日亏缓冲和最大亏损缓冲扣除，再计算下一笔交易预算。

```text
position_size =
    floor(risk_budget / loss_per_volume_unit / volume_step) * volume_step
```

交易量必须向下取整，且小于最小交易量时拒绝。

## 5. 新闻策略

新闻事件至少包含：

```text
event_id
release_time_utc
importance
affected_symbols
source
rule_version
coverage_start
coverage_end
```

日历的“抓取时间新鲜”不等于“内容完整”。新闻覆盖必须包含当前时间前后的全部内部守护窗口；休市覆盖必须延伸到未来禁开仓窗口之外。新鲜但覆盖不足的快照会被当作过期数据，禁止新增风险。

`affected_symbols` 可以使用精确品种、尾部通配符（例如 `EURUSD*` 覆盖经纪商后缀）或全品种 `*`。中间通配符会被输入校验拒绝。

内部预防窗口默认是新闻前后 10 分钟：

- 禁止增加受影响品种风险；
- 取消挂单；
- 禁止新开仓；
- 在 FTMO 硬窗口前主动平仓或减仓，避免止损或止盈在硬窗口内自动触发。

FTMO Standard FTMO Account 的硬窗口由账户规则配置决定，默认前后 2 分钟。Swing 账户和评估阶段不能直接套用同一条新闻规则，必须由账户配置决定。

新闻守护进程必须处理已有仓位，而不是只拦截新订单。对于 Standard FTMO Account：

```text
T - 10 分钟：取消受影响品种挂单，停止增加风险
T - 10 分钟至 T - 2 分钟：主动减仓或平仓
T - 2 分钟至 T + 2 分钟：禁止开仓、平仓以及让 SL/TP 触发
T + 10 分钟：检查点差和流动性后恢复交易
```

## 6. 平台适配约束

### MT5 EA

建议每个账户安装一个 `RiskGuardEA`，策略 EA 通过唯一 `Magic Number` 和 `Idea ID` 标记交易。适配层需要采集：

- `ACCOUNT_BALANCE`、`ACCOUNT_EQUITY`；
- 当前持仓和挂单；
- 历史订单、成交、佣金、Swap；
- `OnTradeTransaction` 交易事件；
- 订单请求与平台返回码。

同一笔交易可能触发多个交易事件，必须使用订单号、成交号和请求 ID 去重。

### cTrader cBot

建议使用统一的 `RiskGuard` cBot 或交易插件。适配层需要采集：

- `Account.Balance`、`Account.Equity`；
- `Positions`、`PendingOrders` 和历史交易；
- 开仓、平仓、修改事件；
- `Label`、策略 ID 和交易思想 ID；
- 平台交易结果和错误码。

平台的交易量单位可能不同，适配层必须先归一化到统一风险单位，再交给核心引擎。

## 7. 周末与长休市

对持续至少 2 小时的休市：

```text
T - 120 分钟：所有阶段禁止新增风险，防止 gap trading
T - 10 分钟：Standard FTMO Account 平仓并取消挂单
T：若仍有 Standard 持仓，触发紧急告警
```

Evaluation 和 Swing 可以跨周末持仓，但仍受禁止 gap trading 的前 2 小时开仓限制。

## 8. 持久化

管理员先调用 `/v1/account-sync` 登记并固定账户类型、阶段、风格、初始资金和经过核对的日界线基线；平台凭证只能同步已登记账户。平台每次评估前调用同一接口更新动态快照。SQLite 保存：

- Prague 日界线和当日开始余额；
- 1-Step 最高结算余额；
- 最近请求、开仓和修改时间；
- 风控允许后的开仓频率预留；
- 平台明确拒绝后释放的预留；
- 当日内部锁和已观察到的官方违规锁；
- 新闻和休市日历快照、内容哈希、抓取时间、覆盖起止时间和规则版本；
- 每个规则版本对应的风险规则配置指纹，防止同名版本被静默改写；
- 账户凭证摘要、作用域、有效期、轮换、撤销和最后使用时间；
- 按阶段与周期隔离的已平仓损益、开仓日和资格历史完整性；
- 在线备份和恢复结果。

重启 EA/cBot 或 API 不会清空频率。

API 进程会在打开 SQLite 前取得 `<state>.server.lock` 的操作系统排他锁，并拒绝不安全的锁文件。在线备份使用 SQLite Backup API，可以在服务运行时执行；恢复必须停机，恢复工具会校验锁是否仍被持有、数据库结构和 `PRAGMA quick_check`，通过后再原子替换目标文件。

Prague 日界线以风控服务器接收账户同步的时间为准。客户端 `as_of` 只用于时钟偏差和快照新鲜度检查；若评估时数据库仍停留在上一 FTMO 日，新增风险会因结算基线不确定而被拒绝。

平台请求 ID 也必须跨重启保持唯一：MT5 使用账户级持久序号与 UTC 秒，cTrader 使用 GUID。服务端仍以 `(account_id, request_id)` 作为幂等边界。

## 9. 凭证边界

管理员令牌与账户凭证分离：

```text
管理员令牌
  -> 日历、健康、指标、凭证管理、全账户资格汇总

账户凭证
  -> 固定 account_id + scopes + not_before + expires_at + revoked_at
```

账户秘密使用 256 位随机值。SQLite 只保存随机盐和 SHA-256 摘要，不保存明文。默认平台凭证仅含账户同步、交易评估、执行回报和日历读取；结算、资格读取和资格写入必须使用独立的显式作用域。轮换在一个事务中创建新凭证并设置旧凭证失效时间，支持短暂重叠，避免计划内轮换中断。`last_used_at` 最多每分钟持久化一次，减少平台守护轮询对 SQLite WAL 的写放大，这不影响每次请求的实时认证。

远程部署可由 Python TLS 层直接要求客户端证书，也可以在受控反向代理终止 mTLS。账户凭证仍用于账户绑定和细粒度作用域。

## 10. 独立资格看板

资格模块不参与交易前闸门。每个资格周期由以下键隔离：

```text
account_id + phase + cycle_id
```

- Profit Target：当前周期已平仓净损益、账户余额达到目标且账户级持仓与挂单库存确认已清零。挂单清零是本系统的保守完整性门槛，不替代 FTMO 对资格的最终审核；
- Best Day Rule：最盈利 FTMO 日 / 所有正收益 FTMO 日之和；
- Minimum Trading Days：CE(S)T 日内至少开过一个仓位的日期数量。

已平仓交易不能替代开仓日事件，因为跨日持仓只计开仓日。历史同步器先写交易和开仓日，再提交 `history_start_at` 与 `complete_through`；完整性水位未覆盖最新账户快照、账户持仓/挂单库存未知或账户快照超过资格新鲜度阈值时，资格结果强制进入 `uncertain`。

浏览器看板只展示 `/v1/qualification/accounts`，不会把管理员令牌写入 URL 或本地存储。

## 11. 可观测性

`/metrics` 将动态管理路由归一为路由模板，所有未知路由归并为 `/__unknown__`，避免账户 ID、凭证 ID、品种和随机 URL 造成高基数或敏感信息泄露。指标覆盖：

- API 请求、状态码和决定代码；
- 日历存在性、年龄、覆盖充足性、同步与恢复；
- 账户状态分布；
- 未知执行结果和账户级新增风险锁；
- 日历配置感知的过期状态、资格快照过期、账户不确定状态和凭证临近过期；
- SQLite `quick_check` 健康和新增风险就绪状态；
- 备份/恢复结果和最后备份年龄。

Prometheus 抓取示例和告警规则放在 `monitoring/`。告警覆盖数据库不可用、服务未就绪、日历缺失/过期/覆盖不足、未知执行、官方 `BREACH`、备份失败和备份逾期。

## 12. 断线和异常

以下情况必须停止新增风险：

- 账户权益超过 5 秒未更新；
- 新闻日历超过 `news_controls.max_calendar_age_seconds` 未同步；
- 长休市日历超过 `market_close_controls.max_schedule_age_seconds` 未同步；
- 日历抓取时间新鲜但未覆盖当前全部风控前视/后视窗口；
- 日界线转换失败；
- 平台订单结果未知；
- 核心风控服务不可用；
- 规则版本不匹配。

恢复交易前必须完成状态重建：余额、权益、持仓、挂单、历史成交、当日最低权益和频率计数全部重新读取。
