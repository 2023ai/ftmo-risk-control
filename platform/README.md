# 平台适配器

## 重要边界

两个适配器已在本机对应平台编译通过，但它们不是经纪商服务器端的强制拦截器：

- MT5 EA 只能守护经过该 EA 的自动交易请求，不能保证拦截终端中所有人工订单。
- cTrader cBot 只能控制经过该 cBot 的交易逻辑，不能替代账户级交易权限或服务器网关。
- 生产环境仍应使用账户权限、经纪商网关或桥接层保证所有新增风险经过风控。

## MT5

文件：[RiskGuardEA.mq5](mt5/RiskGuardEA.mq5)

1. 在 MT5 的 WebRequest 白名单加入风险 API 的完整地址，例如 `http://127.0.0.1:8765`。
2. 先由管理员登记账户基线；再在 EA 参数中填写与该登记一致的账户类型、阶段、账户风格、初始资金、结算基线和当前 MT5 账户对应的 `AccountCredential`，不要使用管理员令牌。
3. 策略调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不能直接调用 `CTrade`。
4. 实盘前在模拟账户测试点值、合约大小、佣金、滑点和交易量步进。

仓库只包含 `RiskGuardEA.mq5` 源码。`.ex5` 和编译日志是本地构建产物，已加入 `.gitignore`，应在目标 MetaTrader 5 环境中重新编译。

## cTrader

文件：[RiskGuardBot.cs](ctrader/RiskGuardBot.cs)

1. 将代码复制到 cTrader Automate 的 cBot 项目。
2. 先由管理员登记账户基线；再填写 API 地址、与该登记一致的参数和当前 cTrader 账户对应的 `AccountCredential`，不要使用管理员令牌。
3. 策略调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`，不能直接调用平台下单函数。
4. 用当前 cTrader 版本编译后，先做回测，再做小额模拟账户回放。

当前源码已在 FTMO Platform cTrader 5.9.140 中构建成功。

## 两个平台都必须实现

- 每次订单请求前读取最新余额、权益、持仓和挂单；
- 账户同步必须回传全账户 `open_positions_count` 与 `pending_orders_count`，不能只统计当前策略或当前图表品种；
- 已有持仓按当前 Bid/Ask 到止损计算剩余权益风险，挂单按目标入场价到止损计算；
- 每次成交后重新读取状态，不使用本地估算作为最终事实；
- 风控 API 超时、非 200、JSON 无法解析时拒绝新增风险；
- 订单结果未知时停止新增风险并触发人工告警；
- 平台明确拒单回传 `failure`；MT5 的 `TRADE_RETCODE_PLACED`、超时和连接异常不视为最终成功，cTrader 的 `Timeout`、`Disconnected` 和 `TechnicalError` 也保持 `unknown`；
- 订单结果未知或执行结果上报失败会在适配器本地和服务端持久化新增风险锁；必须核对平台订单后用原 `request_id` 补报最终结果，不能通过重启自动清除；
- 账户同步失败时，新增风险请求直接拒绝；已授权的平仓、减仓和撤单仍会尝试调用服务端评估，保留应急降风险路径；
- 新闻和休市状态独立处理；一个状态查询失败不能抹掉另一个已确认的平仓或撤单信号；
- 资格同步器必须分别回传已平仓净损益、开仓日事件和历史完整性水位，不能用平仓日期推算 Minimum Trading Days；
- 手动订单、其他 EA/cBot 订单要通过账户权限或服务器网关隔离。
