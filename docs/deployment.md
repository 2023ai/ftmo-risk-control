# 部署与上线检查

## 1. 本地启动

```bash
export RISK_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RISK_AUDIT_PATH="$PWD/runtime/audit.jsonl"
export RISK_STATE_PATH="$PWD/runtime/risk-state.db"
python3 -m src.risk_api \
  --host 127.0.0.1 \
  --port 8765 \
  --config config/ftmo-v2.json
```

服务拒绝在没有 `RISK_API_TOKEN`/`--token` 时启动。生产评估必须使用 `account_id` 和 SQLite 持久化状态；无状态 `/v1/evaluate` 和 `/v1/position-size` 默认关闭，只能在隔离的测试/回放实例中分别使用 `--allow-stateless-evaluate` 和 `--allow-stateless-position-size` 显式开启。

服务在启动时读取并校验一次配置，运行期间固定使用该配置快照。修改规则文件后必须重启服务，并确认 `/health` 中的 `rule_version`；不得依赖运行中直接修改文件。

验证：

```bash
curl -s http://127.0.0.1:8765/health
```

非 loopback 监听默认被拒绝。确需从另一台机器访问时，必须显式增加 `--allow-remote-bind`，并放在专用内网、VPN 或已认证的 TLS 反向代理后面，同时限制来源 IP；不能直接把明文 HTTP 服务暴露到公网。

SQLite 状态文件和 JSONL 审计文件会被服务收紧为仅属主可读写（`0600`）。部署目录仍应由专用系统账户持有，并禁止其他用户读取。

升级已有 SQLite 时服务会自动增加 `day_locked` 和 `breach_latched` 字段。升级前已经发生但当前权益已恢复的历史触线无法由快照重建，首次上线该版本前必须人工核对账户历史和 FTMO 状态。

## 2. 日历同步

`config/news-events.example.json` 只是格式示例，不是实际新闻日历。上线前应由公司人员或已批准的数据源生成并审核 `affected_symbols`。

```bash
python3 scripts/sync_news.py \
  --file config/news-events.example.json \
  --url http://127.0.0.1:8765
```

Standard FTMO Account 的新增风险请求必须有不超过 60 秒的新鲜新闻数据。新闻同步失败时，系统应保持禁止新增风险。

周末和超过 2 小时的休市日历也必须由批准的数据源生成：

```bash
python3 scripts/sync_market.py \
  --file config/market-closures.example.json \
  --url http://127.0.0.1:8765
```

市场日历超过 1 小时未更新时，Standard FTMO Account 禁止新增风险并发出告警。

所有 POST 请求都会返回 `request_id`。设置 `RISK_AUDIT_PATH` 后，服务以 JSON Lines 格式记录请求 ID、规则版本、决定、账户状态、HTTP 状态和错误原因。

日界线前后必须由批准的数据源调用 `/v1/settlement-sync`。如果没有确认的结算余额，账户会进入 `data_uncertain`，只允许降低风险的动作。

内部 `LOCKED` 当日不可自动解除；官方 `BREACH` 不提供在线清除接口。误报或数据修复必须停止服务、保留审计证据并由授权人员处理，不能通过重启 EA/cBot 或 API 绕过。

平台、API 主机和结算数据源都必须启用可靠的 UTC 时间同步。客户端时间只接受 ±30 秒偏差，日界线、新闻、休市、频率和执行活动使用服务器接收时间判定。

MT5 使用终端 Global Variable、cTrader 使用本地存储持久化未知结果锁；未知结果不能通过重启 EA/cBot 自动清除。平台明确拒绝应回传 `failure`，超时、空结果或仅确认订单已受理但尚未得到最终结果时必须回传 `unknown`。

## 3. MT5 接入

- 在 MT5 的工具设置中把风险 API 地址加入 WebRequest 白名单。
- 把 `RiskGuardEA.mq5` 放入 `MQL5/Experts`，用 MetaEditor 编译。
- 仓库只发布 `platform/mt5/RiskGuardEA.mq5` 源码；请在目标 MetaTrader 5 环境中用 MetaEditor 编译，不要把本地 `.ex5` 或 `.log` 文件提交到仓库。
- 策略只能调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不得直接调用 `CTrade`。
- Bootstrap 两个余额参数只在账户第一次注册时使用；之后由 SQLite 状态层按 Prague 日界线维护。
- 交易量、点值、佣金、Swap 和合约货币必须用真实品种规格回放验证。
- 已有持仓风险必须用当前 Bid/Ask 到止损计算，挂单风险使用目标入场价到止损计算。

## 4. cTrader 接入

- 将 `RiskGuardBot.cs` 放入 cTrader Automate 项目并编译。
- 策略只能调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`。
- 使用当前账户的实际品种规格验证 `AmountRisked()`、交易量单位和最小步进。
- 对平仓、修改止损、取消挂单也接入相同的 API 检查和审计流程。
- 新闻与休市状态分别判断；一个状态接口失败时，另一个已确认的平仓或撤单信号仍必须执行，同时对缺失状态告警。

## 5. 上线前必须通过

```text
[ ] 1-Step 和 2-Step 各一组账户回放
[ ] Evaluation、FTMO Account Standard、Swing 各一组回放
[ ] Prague 夏令时切换
[ ] 00:00 CE(S)T 日界线重置
[ ] 余额盈利后 1-Step 最大亏损追踪
[ ] 浮动亏损触及内部停止线
[ ] 浮动亏损触及官方底线
[ ] 新闻前 10 分钟清仓和挂单取消
[ ] 新闻前后 2 分钟禁止新增或平仓指令
[ ] 点差扩大和滑点
[ ] 部分成交
[ ] 订单结果未知
[ ] 风控服务断线
[ ] 新闻同步超过 60 秒
[ ] 周末/超过 2 小时休市前 10 分钟平仓和撤单
[ ] 市场休市日历超过 1 小时
[ ] 人工订单和其他 EA/cBot 订单隔离
[ ] MT5 与 cTrader 相同输入得到相同风控决定
[ ] 账户个人访问权限、设备和 IP 符合 FTMO 要求
[ ] 禁止跨账户反向对冲、共享账号和第三方代交易
```

## 6. 不能自动化保证的事项

客户端 EA/cBot 不能替代服务器端订单权限。若账户仍允许人工终端或其他程序直接下单，系统只能做到监控和事后冻结，不能证明所有订单在下单前都被拦截。

禁止交易方法、账号共享、跨账户协调和第三方代交易不能仅靠本项目自动证明合规，必须执行 [合规矩阵](compliance-matrix.md) 中的组织控制。
