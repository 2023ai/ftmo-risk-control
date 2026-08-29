# FTMO 自营交易风控系统

这是一个面向 FTMO 账户、MT5 和 cTrader 的可运行 V5 风控实现。

本项目是风控参考实现和平台接入模板，不构成 FTMO 官方软件、法律意见或实盘收益保证。上线前必须使用公司批准的数据源、目标账户规格和模拟账户回放验证。

当前版本聚焦十一类核心能力：

1. 以损定仓：本系统内部先确定止损和允许亏损，再计算交易量；这是内部风控门槛，不是对 FTMO 官方止损政策的替代。
2. 新闻时段：区分 FTMO 账户阶段、Standard/Swing 账户和受影响品种。
3. 交易频率：限制短周期开仓、日内开仓和服务器请求数量。
4. 日亏与最大亏损：按账户规则计算官方底线，并使用更保守的内部停止线。
5. 周末与长休市：全阶段提前 2 小时禁止开仓，Standard FTMO Account 提前平仓撤单。
6. 持久化审计：SQLite 保存日界线、结算确认、频率、风控决定幂等记录与平台执行结果。
7. 日历韧性：新闻和休市快照持久化，校验内容哈希与覆盖时间，重启自动恢复，支持在线备份和停机恢复。
8. 账户安全：管理员令牌与账户凭证分离，支持作用域、轮换、重叠窗口、过期、撤销和可选 mTLS。
9. 资格看板：按阶段和周期独立计算 Profit Target、Minimum Trading Days 和 Best Day Rule。
10. 资格完整性：Profit Target 同时核对账户余额、账户级持仓/挂单库存和快照新鲜度；无法确认时显示需复核。
11. 执行闭环：未知执行结果在服务器端形成账户级新增风险锁，最终核对后可幂等解除。

内部日亏锁一旦触发会保持到下一 FTMO 日；观察到官方亏损底线后会持久锁定，权益反弹或重启服务都不会自动恢复新增风险。

## 目录

```text
config/ftmo-v2.json       规则参数和默认内部阈值
config/news-events.example.json
                          新闻事件格式示例
config/market-closures.example.json
                          长休市/周末收市格式示例
config/qualification-history.example.json
                          资格历史批量同步格式示例
docs/architecture.md      系统架构、状态机和平台接入要求
docs/api-contract.md      风控 API 请求和响应契约
docs/compliance-matrix.md 规则覆盖和组织控制边界
docs/deployment.md        部署与上线检查
docs/verification.md      自动测试与平台编译结果
dashboard/qualification.html
                          浏览器资格看板
monitoring/               Prometheus 抓取示例和告警规则
SECURITY.md               密钥、账户数据和漏洞报告规范
CONTRIBUTING.md           开发、测试和平台验证要求
LICENSE                   MIT 许可证
src/risk_engine.py        可独立测试的纯 Python 风控引擎
src/risk_api.py           本地 HTTP 风控服务
src/state_store.py        SQLite 账户、日界线和频率状态
src/qualification.py      独立资格计算
platform/                 MT5 EA 和 cTrader cBot 接入模板
scripts/sync_news.py      将人工审核后的新闻映射推送到服务
scripts/sync_market.py    将审核后的长休市日历推送到服务
scripts/sync_qualification.py
                          上传已平仓损益、开仓日和完整性水位
scripts/backup_state.py   SQLite 在线一致性备份
scripts/restore_state.py  停机校验恢复
requirements-dev.txt      Ruff 和 mypy 开发检查版本
tests/test_risk_engine.py 关键规则测试
tests/test_risk_api.py    HTTP API 集成测试
tests/test_state_store.py SQLite 重启恢复测试
```

## 运行测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -W error::ResourceWarning -m unittest discover -s tests -v
make lint
```

## 启动本地风控服务

```bash
export RISK_API_TOKEN='replace-with-a-long-random-token'
export RISK_AUDIT_PATH='/var/log/ftmo-risk/audit.jsonl'
python3 -m src.risk_api --config config/ftmo-v2.json
```

`RISK_API_TOKEN` 是管理员令牌，只用于日历、监控、账户首次登记、资格汇总和凭证管理。生产平台不保存管理员令牌。生产 API 必须使用持久化 SQLite `--state` 路径；`:memory:` 仅供直接 `StateStore` 单元测试使用。管理员必须先用带已核对余额的 `/v1/account-sync` 登记账户基线，之后才能签发独立平台凭证；请求字段见 [API 契约](docs/api-contract.md#post-v1account-sync)：

```bash
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://127.0.0.1:8765/v1/admin/accounts/mt5-10001/credentials
```

响应中的 `secret` 只显示一次，填入 MT5/cTrader 的 `AccountCredential`。默认凭证仅含 `account:sync`、`trade:evaluate`、`trade:execution` 和 `calendar:read`；结算、资格同步和账户级资格读取必须另签发相应作用域。生产 `/v1/evaluate` 必须使用 `account_id` 和 `X-Account-Credential`；管理员令牌不能代替账户凭证提交交易评估。

新闻事件必须先经过人工审核和品种映射，再同步：

```bash
python3 scripts/sync_news.py \
  --file config/news-events.example.json
```

所有生产账户都需要同步周末和超过 2 小时的休市时段，用于全阶段提前禁止新增风险；Standard FTMO Account 另外执行提前平仓撤单：

```bash
python3 scripts/sync_market.py \
  --file config/market-closures.example.json
```

两种日历文件都必须包含 `coverage_start` 和 `coverage_end`。新闻覆盖至少要包含当前时间前后的内部新闻窗口，休市日历要覆盖未来的禁开仓窗口。抓取时间虽新但覆盖不足时，服务仍会 fail-closed。`affected_symbols` 支持精确品种、尾部通配符（如 `EURUSD*`）和全品种 `*`。新闻示例故意使用 `*` 作为保守占位；只有在已批准映射完整覆盖该事件的全部 FTMO 受影响资产和经纪商品种后缀后，才能缩小范围，不能直接照抄不完整的品种列表。

浏览器资格看板：

```text
http://127.0.0.1:8765/dashboard/qualification
```

看板使用当前页面内存中的管理员令牌读取 `/v1/qualification/accounts`，不会把令牌写入 URL 或浏览器存储。资格历史必须按 `phase + cycle_id` 同步已平仓净损益、开仓日事件和完整性水位；缺少完整性确认时只显示“需复核”。

批量同步审核后的资格历史前，需为该账户签发仅含 `qualification:write` 的独立凭证：

```bash
export RISK_ACCOUNT_CREDENTIAL='one-account-secret'
python3 scripts/sync_qualification.py \
  --file config/qualification-history.example.json
```

在线备份和停机恢复：

```bash
python3 scripts/backup_state.py \
  --state runtime/risk-state.db \
  --output backups/risk-state-2026-08-26.db

python3 scripts/restore_state.py \
  --source backups/risk-state-2026-08-26.db \
  --state runtime/risk-state.db
```

`GET /health` 是服务存活信息；`GET /ready` 是新增风险就绪检查，SQLite 或任一必需日历缺失、过期、版本不匹配或覆盖不足时返回 `503`。`GET /metrics` 提供 Prometheus 文本指标；示例抓取配置和告警规则位于 `monitoring/`。

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
- 日界线由风控服务器接收时间决定；平台时间仅用于 ±30 秒时钟健康检查。
- 已有持仓按当前可平仓报价到止损计算剩余权益风险，挂单按目标入场价到止损计算。
- 新闻数据必须保存来源、发布时间、影响品种、覆盖时间和规则版本。
- 长休市日历必须来自实际 FTMO/经纪商品种交易时间，示例文件不能直接用于实盘。
- 所有时间统一使用带时区的 ISO 8601 时间。
- 规则变更必须增加 `rule_version`，不得静默覆盖历史审计记录；服务会持久化风险规则指纹，同一版本出现不同风险配置时自动停止新增风险。
- Minimum Trading Days 使用 Prague/CE(S)T 开仓日：当天至少开过一个仓位计 1 天，持仓跨日不重复计数。
- `ALLOW` 只表示当前交易请求通过风控，不表示账户已经满足 Profit Target、Minimum Trading Days 或 Best Day Rule。看板额外要求同步的账户余额达到目标、`open_positions_count` 和 `pending_orders_count` 都为零；挂单清零是本系统的保守完整性门槛，不是对 FTMO 官方资格审核的替代。
- 平台同步时间戳必须带毫秒精度或单调递增；同一时间戳上传冲突账户快照会被拒绝。

完整上线步骤见 [部署检查](docs/deployment.md)，实际验证结果见 [验证报告](docs/verification.md)。

## 开源边界

- 仓库只包含源码、示例配置和测试；运行时 SQLite、审计日志、环境文件和 MT5 编译产物不会提交。
- 不要提交 `RISK_API_TOKEN`、账户凭证明文、证书私钥、真实账户标识、真实交易记录、经纪商凭据或任何个人数据。
- 适配器无法替代账户级交易权限或服务器网关；必须确保所有新增风险路径都经过风控。
- 资格目标由独立看板监控，不会自动改变单笔交易闸门；历史不完整时禁止把看板结果解释为正式通过资格。

安全问题请先阅读 [安全政策](SECURITY.md)，开发流程见 [贡献指南](CONTRIBUTING.md)。
