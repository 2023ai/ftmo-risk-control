# 验证报告

验证日期：2026-08-29。

## 自动测试

```text
214 tests passed
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
- 同一已批准 `request_id` 在收到执行结果前也拒绝重放，不会再次返回 `ALLOW`。
- 守护端时钟偏差、远程监听显式授权、配置启动快照和敏感文件权限。
- 明确失败的修改释放冷却预留，无状态仓位计算默认关闭。
- 当前报价到止损的开放风险扣减、服务器日界线权威和请求量预警。
- 执行时间偏差、未来活动清理隔离、重复日历 ID 和同时间戳内容冲突。
- 日内 `LOCKED` 跨权益反弹持续到下一 FTMO 日，官方 `BREACH` 持久锁定。
- 新闻和休市日历 SQLite 持久化、内容冲突拒绝和 API 重启恢复。
- 日历覆盖起止时间、新鲜但覆盖不足时 fail-closed、经纪商品种后缀通配。
- 日历 V2 安全元数据哈希，事件内容或覆盖边界被改动时拒绝恢复。
- 旧版日历哈希不能通过补写覆盖边界升级为可信快照。
- SQLite 在线备份、`0600`、完整性校验、恢复和运行中服务锁拒绝。
- SQLite `PRAGMA user_version`、必需表/字段校验和不健康状态拒绝备份。
- 服务锁在打开 SQLite 前取得，拒绝第二个服务进程与不安全的锁文件触碰状态库。
- 每账户凭证绑定、作用域、跨账户拒绝、轮换重叠、过期、撤销和只显示一次的秘密。
- 管理员首次登记账户基线、默认平台凭证最小权限、孤儿凭证无法建立账户。
- JSON 重复对象键拒绝，避免同一请求的字段歧义。
- MT5/cTrader 响应身份校验、有限数值校验和止损方向校验。
- 新闻/休市查询按品种去重，避免重复事件触发重复守护动作。
- Prometheus 数据库、日历、决定、账户状态、未知执行和备份指标。
- Prometheus 动态路由标签归一、日历覆盖指标和新增风险就绪指标。
- 审计日志路径、服务锁和状态目录创建后的完整路径安全复核。
- SQLite 备份/恢复失败时的 WAL/SHM sidecar 回滚，以及 macOS /var 和 /tmp 系统别名兼容。
- 服务端未知执行锁、最终结果幂等结算、账户级持仓/挂单库存、同时间戳快照冲突和内存状态库。
- MT5/cTrader 本地未知执行锁必须等待服务器 `reconciliation_complete=true` 才能清除；Standard 活动中的长休市也会触发 `force_flat`。
- TLS-only 与 mTLS 客户端证书要求分别报告，不能把普通 TLS 误报为 mTLS。
- 配置感知的日历过期指标、凭证临近过期指标、失败备份后的最后成功备份年龄。
- `/ready` 在持久化状态或日历未就绪时返回 `503`，完整就绪时返回 `200`。
- 凭证认证仍逐请求执行，`last_used_at` 持久化写入按分钟节流。
- 非法或重复的凭证列表查询返回稳定 `400`，不会中断 HTTP 连接。
- Qualification 的阶段/周期隔离、历史完整性、Best Day、Profit Target 和基于开仓日的 Minimum Trading Days。
- 浏览器资格看板静态入口和受认证的账户汇总 API。
- 资格看板在 1440x900 和 390x844 视口检查通过，页面无横向溢出；明细表在移动端使用受控横向滚动。

## MT5

当前环境未找到 `metaeditor64.exe`，本轮未把旧的 `.ex5` 或 `.log` 文件当作验证证据。仓库发布的是 [RiskGuardEA.mq5](../platform/mt5/RiskGuardEA.mq5) 源码；必须在目标 MetaTrader 5 环境中重新编译，并验证毫秒时间戳、账户级持仓/挂单同步和执行上报失败锁。

## cTrader

使用 FTMO Platform cTrader 的 `cAlgo.API.dll` 和本机 .NET SDK 对当前源码重新构建：

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
- 未使用公司 PKI 执行真实 mTLS 证书握手；本地自签 CA 已验证有客户端证书可握手，自动测试仍覆盖缺少证书/密钥时的启动拒绝。

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
audit rule_version: ftmo-v5-2026-08-26
readiness: ready_for_risk_increase=true
calendar restart restore: passed
account credential isolation and rotation: passed
qualification dashboard API: passed
backup and restore round trip: passed
Prometheus metrics: passed
server-side unknown execution lock and resolution: passed
account position/pending-order inventory: passed
```
