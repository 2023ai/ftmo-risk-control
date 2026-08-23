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

服务拒绝在没有 `RISK_API_TOKEN`/`--token` 时启动。生产评估必须使用 `account_id` 和 SQLite 持久化状态；无状态 `/v1/evaluate` 只应在测试/回放服务器显式开启。

验证：

```bash
curl -s http://127.0.0.1:8765/health
```

如果需要从另一台机器访问，不能直接把服务暴露到公网。应放在专用内网、VPN 或已认证的反向代理后面，并限制来源 IP。

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

MT5 使用终端 Global Variable、cTrader 使用本地存储持久化未知结果锁；未知结果不能通过重启 EA/cBot 自动清除。

## 3. MT5 接入

- 在 MT5 的工具设置中把风险 API 地址加入 WebRequest 白名单。
- 把 `RiskGuardEA.mq5` 放入 `MQL5/Experts`，用 MetaEditor 编译。
- 仓库只发布 `platform/mt5/RiskGuardEA.mq5` 源码；请在目标 MetaTrader 5 环境中用 MetaEditor 编译，不要把本地 `.ex5` 或 `.log` 文件提交到仓库。
- 策略只能调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不得直接调用 `CTrade`。
- Bootstrap 两个余额参数只在账户第一次注册时使用；之后由 SQLite 状态层按 Prague 日界线维护。
- 交易量、点值、佣金、Swap 和合约货币必须用真实品种规格回放验证。

## 4. cTrader 接入

- 将 `RiskGuardBot.cs` 放入 cTrader Automate 项目并编译。
- 策略只能调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`。
- 使用当前账户的实际品种规格验证 `AmountRisked()`、交易量单位和最小步进。
- 对平仓、修改止损、取消挂单也接入相同的 API 检查和审计流程。

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
