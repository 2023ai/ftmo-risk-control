# FTMO 自营交易风控系统

这是一个面向 FTMO 账户、MT5 和 cTrader 的可运行 V2 风控实现。

本项目是风控参考实现和平台接入模板，不构成 FTMO 官方软件、法律意见或实盘收益保证。上线前必须使用公司批准的数据源、目标账户规格和模拟账户回放验证。

当前版本聚焦四类核心控制：

1. 以损定仓：先确定止损和允许亏损，再计算交易量。
2. 新闻时段：区分 FTMO 账户阶段、Standard/Swing 账户和受影响品种。
3. 交易频率：限制短周期开仓、日内开仓和服务器请求数量。
4. 日亏与最大亏损：按账户规则计算官方底线，并使用更保守的内部停止线。
5. 周末与长休市：全阶段提前 2 小时禁止开仓，Standard FTMO Account 提前平仓撤单。
6. 持久化审计：SQLite 保存日界线、结算确认、频率、风控决定幂等记录与平台执行结果。

## 目录

```text
config/ftmo-v2.json       规则参数和默认内部阈值
config/news-events.example.json
                          新闻事件格式示例
config/market-closures.example.json
                          长休市/周末收市格式示例
docs/architecture.md      系统架构、状态机和平台接入要求
docs/api-contract.md      风控 API 请求和响应契约
docs/compliance-matrix.md 规则覆盖和组织控制边界
docs/deployment.md        部署与上线检查
docs/verification.md      自动测试与平台编译结果
SECURITY.md               密钥、账户数据和漏洞报告规范
CONTRIBUTING.md           开发、测试和平台验证要求
LICENSE                   MIT 许可证
src/risk_engine.py        可独立测试的纯 Python 风控引擎
src/risk_api.py           本地 HTTP 风控服务
src/state_store.py        SQLite 账户、日界线和频率状态
platform/                 MT5 EA 和 cTrader cBot 接入模板
scripts/sync_news.py      将人工审核后的新闻映射推送到服务
scripts/sync_market.py    将审核后的长休市日历推送到服务
tests/test_risk_engine.py 关键规则测试
tests/test_risk_api.py    HTTP API 集成测试
tests/test_state_store.py SQLite 重启恢复测试
```

## 运行测试

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
```

## 启动本地风控服务

```bash
export RISK_API_TOKEN='replace-with-a-long-random-token'
export RISK_AUDIT_PATH='/var/log/ftmo-risk/audit.jsonl'
python3 -m src.risk_api --config config/ftmo-v2.json
```

没有 `RISK_API_TOKEN` 时服务不会启动。生产环境的 `/v1/evaluate` 必须使用 `account_id`；无状态评估仅用于显式开启的测试/回放实例。

新闻事件必须先经过人工审核和品种映射，再同步：

```bash
python3 scripts/sync_news.py \
  --file config/news-events.example.json
```

Standard FTMO Account 还需要同步周末和超过 2 小时的休市时段：

```bash
python3 scripts/sync_market.py \
  --file config/market-closures.example.json
```

## 使用边界

`src/risk_engine.py` 不直接连接交易平台。MT5 EA 或 cTrader cBot 负责：

- 采集账户余额、权益、持仓、挂单和历史成交；
- 将平台的合约规格转换为统一的货币风险；
- 调用风控引擎；
- 只有在引擎返回 `ALLOW` 时提交交易请求；
- 记录每一次允许、拒绝、异常和实际成交。

平台客户端不是服务器级拦截层。生产环境必须隔离人工订单和其他 EA/cBot，或使用账户权限、交易网关/桥接层保证所有新增风险经过风控。

## 重要配置要求

- 每个账户必须单独配置：账户类型、阶段、Standard/Swing、初始资金和日界线。
- 日亏计算使用 FTMO 的 CE(S)T 日界线；系统实现使用 `Europe/Prague` 时区。
- 新闻数据必须保存来源、发布时间、影响品种和规则版本。
- 长休市日历必须来自实际 FTMO/经纪商品种交易时间，示例文件不能直接用于实盘。
- 所有时间统一使用带时区的 ISO 8601 时间。
- 规则变更必须增加 `rule_version`，不得静默覆盖历史审计记录。

完整上线步骤见 [部署检查](docs/deployment.md)，实际验证结果见 [验证报告](docs/verification.md)。

## 开源边界

- 仓库只包含源码、示例配置和测试；运行时 SQLite、审计日志、环境文件和 MT5 编译产物不会提交。
- 不要提交 `RISK_API_TOKEN`、真实账户标识、真实交易记录、经纪商凭据或任何个人数据。
- 适配器无法替代账户级交易权限或服务器网关；必须确保所有新增风险路径都经过风控。

安全问题请先阅读 [安全政策](SECURITY.md)，开发流程见 [贡献指南](CONTRIBUTING.md)。
