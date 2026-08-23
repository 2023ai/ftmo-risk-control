# 验证报告

验证日期：2026-08-22。

## 自动测试

```text
68 tests passed
ResourceWarning treated as error
Python source compilation passed
JSON configuration validation passed
git diff --check passed
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

## MT5

使用本机 MetaTrader 5 的 `metaeditor64.exe` 编译：

```text
Result: 0 errors, 0 warnings
Target: X64 Regular
```

编译产物仅用于本地验证，不提交到仓库；仓库发布的是 [RiskGuardEA.mq5](../platform/mt5/RiskGuardEA.mq5) 源码。

## cTrader

使用 FTMO Platform cTrader `5.9.140` 的 Automate Build：

```text
构建成功
```

验证项目名为 `FTMORiskGuardValidation`。只执行了源码构建，没有启动 cBot、没有发送订单。

## 未执行

- 未连接 FTMO 模拟账户执行真实下单回放；
- 未验证具体账户的佣金、点值、滑点和所有品种交易时间；
- 未启用服务器端网关拦截人工交易。

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
audit rule_version: ftmo-v2-2026-08-22
```
