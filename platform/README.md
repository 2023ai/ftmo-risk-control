# 平台适配器

## 重要边界

两个适配器已在本机对应平台编译通过，但它们不是经纪商服务器端的强制拦截器：

- MT5 EA 只能守护经过该 EA 的自动交易请求，不能保证拦截终端中所有人工订单。
- cTrader cBot 只能控制经过该 cBot 的交易逻辑，不能替代账户级交易权限或服务器网关。
- 生产环境仍应使用账户权限、经纪商网关或桥接层保证所有新增风险经过风控。

## MT5

文件：[RiskGuardEA.mq5](mt5/RiskGuardEA.mq5)

1. 在 MT5 的 WebRequest 白名单加入风险 API 的完整地址，例如 `http://127.0.0.1:8765`。
2. 在 EA 参数中填写账户类型、阶段、账户风格、初始资金、当日开始余额和最高结算余额。
3. 策略调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不能直接调用 `CTrade`。
4. 实盘前在模拟账户测试点值、合约大小、佣金、滑点和交易量步进。

仓库只包含 `RiskGuardEA.mq5` 源码。`.ex5` 和编译日志是本地构建产物，已加入 `.gitignore`，应在目标 MetaTrader 5 环境中重新编译。

## cTrader

文件：[RiskGuardBot.cs](ctrader/RiskGuardBot.cs)

1. 将代码复制到 cTrader Automate 的 cBot 项目。
2. 填写 API 地址和 `RISK_API_TOKEN`。
3. 策略调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`，不能直接调用平台下单函数。
4. 用当前 cTrader 版本编译后，先做回测，再做小额模拟账户回放。

当前源码已在 FTMO Platform cTrader 5.9.140 中构建成功。

## 两个平台都必须实现

- 每次订单请求前读取最新余额、权益、持仓和挂单；
- 已有持仓按当前 Bid/Ask 到止损计算剩余权益风险，挂单按目标入场价到止损计算；
- 每次成交后重新读取状态，不使用本地估算作为最终事实；
- 风控 API 超时、非 200、JSON 无法解析时拒绝新增风险；
- 订单结果未知时停止新增风险并触发人工告警；
- 平台明确拒单回传 `failure`；MT5 的 `TRADE_RETCODE_PLACED`、超时和连接异常不视为最终成功，cTrader 的 `Timeout`、`Disconnected` 和 `TechnicalError` 也保持 `unknown`；
- 订单结果未知会在适配器本地持久化新增风险锁，必须人工核对账户后再清除；
- 新闻和休市状态独立处理；一个状态查询失败不能抹掉另一个已确认的平仓或撤单信号；
- 手动订单、其他 EA/cBot 订单要通过账户权限或服务器网关隔离。
