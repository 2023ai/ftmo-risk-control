# 验证报告

验证日期：2026-08-23。

## 自动测试

```text
113 tests passed
ResourceWarning treated as error
Python source compilation passed
JSON configuration validation passed
git diff --check passed
Ruff passed
mypy passed
```

测试覆盖：

- 1-Step / 2-Step 日亏和最大亏损；
- Prague 日界线与 SQLite 重启恢复；
- 以损定仓、AMBER 减半、RED/LOCKED 拒绝；
- 新闻硬窗口、提前平仓和过期日历；
- 周末/长休市前 2 小时禁开和 Standard 提前平仓；
- 交易频率、并发频率预留、失败订单释放频率预留；
- HTTP 认证、非法输入和执行结果审计。
- 亏损线触及或快照过期时仍允许风险降低动作。
- 请求幂等、未知执行结果保留预留、日结算确认和风险增加型修改。
- 守护端时钟偏差、远程监听显式授权、配置启动快照和敏感文件权限。
- 明确失败的修改释放冷却预留，无状态仓位计算默认关闭。
- 当前报价到止损的开放风险扣减、服务器日界线权威和请求量预警。
- 执行时间偏差、未来活动清理隔离、重复日历 ID 和同时间戳内容冲突。
- 日内 `LOCKED` 跨权益反弹持续到下一 FTMO 日，官方 `BREACH` 持久锁定。
- 新闻和休市日历 SQLite 持久化、内容冲突拒绝和 API 重启恢复。
- SQLite 在线备份、`0600`、完整性校验、恢复和运行中服务锁拒绝。
- 每账户凭证绑定、作用域、跨账户拒绝、轮换重叠、过期、撤销和只显示一次的秘密。
- Prometheus 数据库、日历、决定、账户状态、未知执行和备份指标。
- Qualification 的阶段/周期隔离、历史完整性、Best Day、Profit Target 和基于开仓日的 Minimum Trading Days。
- 浏览器资格看板静态入口和受认证的账户汇总 API。
- 资格看板在 1440x900 和 390x844 视口检查通过，页面无横向溢出；明细表在移动端使用受控横向滚动。

## MT5

使用本机 MetaTrader 5 的 `metaeditor64.exe` 重新编译：

```text
Result: 0 errors, 0 warnings
Target: X64 Regular
```

编译产物仅用于本地验证，不提交到仓库；仓库发布的是 [RiskGuardEA.mq5](../platform/mt5/RiskGuardEA.mq5) 源码。

## cTrader

使用 FTMO Platform cTrader 的 `cAlgo.API.dll` 和 .NET SDK `10.0.302` 对 `net9.0` 重新构建：

```text
0 errors
0 warnings
```

只执行了源码构建，没有启动 cBot、没有发送订单。

## 未执行

- 未连接 FTMO 模拟账户执行真实下单回放；
- 未验证具体账户的佣金、点值、滑点和所有品种交易时间；
- 未启用服务器端网关拦截人工交易。
- 未连接生产 Prometheus/Alertmanager 通知路由。
- 未使用公司 PKI 执行真实 mTLS 证书握手；自动测试覆盖缺少证书/密钥时的启动拒绝。

这些项目必须在上线检查中使用公司的目标账户和批准数据源完成。

## 本地端到端

真实启动本地 API 后完成：

```text
health: persistent_state=true
news sync: accepted
market sync: accepted
account sync: accepted
evaluation decision: ALLOW
audit request_id: smoke-evaluate
audit rule_version: ftmo-v3-2026-08-23
calendar restart restore: passed
account credential isolation and rotation: passed
qualification dashboard API: passed
backup and restore round trip: passed
Prometheus metrics: passed
```
