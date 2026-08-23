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

服务拒绝在没有 `RISK_API_TOKEN`/`--token` 时启动。该值是管理员令牌，不得配置到 MT5/cTrader。生产评估必须使用 `account_id`、账户独立凭证和 SQLite 持久化状态；无状态 `/v1/evaluate` 和 `/v1/position-size` 默认关闭。

服务在启动时读取并校验一次配置，运行期间固定使用该配置快照。修改规则文件后必须重启服务，并确认 `/health` 中的 `rule_version`；不得依赖运行中直接修改文件。

验证：

```bash
curl -s \
  -H "Authorization: Bearer $RISK_API_TOKEN" \
  http://127.0.0.1:8765/health
```

非 loopback 监听默认被拒绝。确需从另一台机器访问时，必须显式增加 `--allow-remote-bind`，并放在专用内网、VPN 或已认证的 TLS 反向代理后面，同时限制来源 IP；不能直接把明文 HTTP 服务暴露到公网。

SQLite 状态文件和 JSONL 审计文件会被服务收紧为仅属主可读写（`0600`）。部署目录仍应由专用系统账户持有，并禁止其他用户读取。

## 2. 账户凭证

每个账户在平台接入前签发独立凭证：

```bash
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "expires_at": "2026-09-22T12:00:00+00:00"
  }' \
  http://127.0.0.1:8765/v1/admin/accounts/mt5-10001/credentials
```

把响应中的一次性 `secret` 放入目标平台的 `AccountCredential`。禁止把它写进仓库、截图、工单或共享密码库以外的位置。

轮换：

```bash
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"overlap_seconds":300}' \
  http://127.0.0.1:8765/v1/admin/credentials/CREDENTIAL_ID/rotate
```

先把新秘密部署到平台，确认账户同步成功，再等待重叠窗口结束。紧急撤销调用 `/v1/admin/credentials/{credential_id}/revoke`。列表接口不返回秘密。

默认凭证 TTL 为 30 天，最大 90 天，轮换重叠 5 分钟；均由 `config/ftmo-v2.json` 控制。Prometheus 应对即将过期凭证另接公司的证书/密钥管理系统；本服务不会发送含凭证信息的通知。

升级已有 SQLite 时服务会自动迁移新增字段和表。升级前已经发生但当前权益已恢复的历史触线无法由快照重建，首次上线该版本前必须人工核对账户历史和 FTMO 状态。

## 3. 可选 mTLS

直接在 Python 服务启用：

```bash
python3 -m src.risk_api \
  --host 0.0.0.0 \
  --allow-remote-bind \
  --tls-cert /etc/ftmo-risk/server.crt \
  --tls-key /etc/ftmo-risk/server.key \
  --tls-ca /etc/ftmo-risk/client-ca.crt \
  --require-client-cert
```

生产更常见的方案是在 Nginx/Envoy/HAProxy 终止 mTLS，再把服务绑定到 loopback 或私有 Unix/网络边界。无论哪种方案，仍需账户凭证完成 `account_id` 和作用域绑定。证书、私钥和 CA 文件必须由专用系统账户读取，权限不高于 `0600`。

## 4. 日历同步

`config/news-events.example.json` 只是格式示例，不是实际新闻日历。上线前应由公司人员或已批准的数据源生成并审核 `affected_symbols`。

```bash
python3 scripts/sync_news.py \
  --file config/news-events.example.json \
  --url http://127.0.0.1:8765
```

Standard FTMO Account 的新增风险请求必须有不超过 60 秒的新鲜新闻数据。同步成功后 SQLite 保存快照，API 重启会自动恢复；恢复不改变原 `fetched_at`，因此过期快照仍会 fail-closed。

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

## 5. 备份与恢复

在线备份：

```bash
python3 scripts/backup_state.py \
  --state "$RISK_STATE_PATH" \
  --output "/var/backups/ftmo-risk/risk-state-$(date -u +%F).db"
```

脚本使用 SQLite Backup API，不复制 WAL/SHM 文件；临时备份通过 `PRAGMA quick_check` 后才原子替换目标，并设置 `0600`。建议至少每日一次并由主机级备份系统复制到不同故障域。

恢复必须先停止 API：

```bash
systemctl stop ftmo-risk-api
python3 scripts/restore_state.py \
  --source /var/backups/ftmo-risk/risk-state-2026-08-23.db \
  --state "$RISK_STATE_PATH"
systemctl start ftmo-risk-api
```

运行中的 API 会持有 `.server.lock`，恢复工具检测到锁时拒绝覆盖。恢复后检查 `/health`、日历时间、账户状态、未知执行和资格历史，再开放平台连接。

## 6. Prometheus 与告警

将 `monitoring/prometheus.example.yml` 中的管理员令牌文件路径替换为受保护文件，并加载 `monitoring/alerts.yml`。令牌文件必须 `0600`，Prometheus 使用 Bearer 头抓取 `/metrics`。

最低告警闭环：

- 数据库不可用：立即停止新增风险并升级为 critical；
- 新闻/休市缺失或过期：保持 fail-closed，检查同步器；
- 未知执行：人工核对订单和频率预留；
- 官方 `BREACH`：冻结账户并启动合规复核；
- 备份失败或超过 25 小时：检查容量、权限和异地副本。

## 7. 资格历史与看板

平台历史同步器按当前 `phase + cycle_id` 执行：

1. `/v1/closed-trade-sync`：已平仓净损益，必须含佣金和 Swap；
2. `/v1/trading-day-sync`：每个开仓事件，服务按 Prague 日期去重；
3. `/v1/qualification-history-sync`：最后提交完整性水位。

人工审核导出或公司历史管道可以使用批量脚本：

```bash
export RISK_ACCOUNT_CREDENTIAL='one-account-secret'
python3 scripts/sync_qualification.py \
  --file config/qualification-history.example.json
```

Evaluation、Verification 和每个 Reward 周期使用不同 `cycle_id`。阶段切换后先完成新周期全量历史同步，再使用浏览器打开：

```text
http://127.0.0.1:8765/dashboard/qualification
```

`uncertain` 账户不得用于正式资格判断。看板不会改变交易闸门；普通交易是否 `ALLOW` 与资格是否达标相互独立。

## 8. MT5 接入

- 在 MT5 的工具设置中把风险 API 地址加入 WebRequest 白名单。
- 把 `RiskGuardEA.mq5` 放入 `MQL5/Experts`，用 MetaEditor 编译。
- 仓库只发布 `platform/mt5/RiskGuardEA.mq5` 源码；请在目标 MetaTrader 5 环境中用 MetaEditor 编译，不要把本地 `.ex5` 或 `.log` 文件提交到仓库。
- 策略只能调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不得直接调用 `CTrade`。
- Bootstrap 两个余额参数只在账户第一次注册时使用；之后由 SQLite 状态层按 Prague 日界线维护。
- `AccountCredential` 只能使用当前 MT5 账户对应的秘密，不能复用管理员令牌或其他账户凭证。
- 交易量、点值、佣金、Swap 和合约货币必须用真实品种规格回放验证。
- 已有持仓风险必须用当前 Bid/Ask 到止损计算，挂单风险使用目标入场价到止损计算。

## 9. cTrader 接入

- 将 `RiskGuardBot.cs` 放入 cTrader Automate 项目并编译。
- 策略只能调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`。
- 使用当前账户的实际品种规格验证 `AmountRisked()`、交易量单位和最小步进。
- 对平仓、修改止损、取消挂单也接入相同的 API 检查和审计流程。
- 新闻与休市状态分别判断；一个状态接口失败时，另一个已确认的平仓或撤单信号仍必须执行，同时对缺失状态告警。
- `AccountCredential` 只能使用当前 cTrader 账户对应的秘密。

## 10. 上线前必须通过

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
[ ] API 重启恢复新闻和休市快照
[ ] 在线备份、停机恢复和 `.server.lock` 拒绝覆盖
[ ] Prometheus 抓取和所有 critical 告警路由
[ ] 每账户凭证跨账户拒绝、轮换、过期和撤销
[ ] mTLS 握手和无证书拒绝（若启用）
[ ] Evaluation/Verification/Reward 周期隔离
[ ] Minimum Trading Days 使用开仓日，不使用平仓日
[ ] 资格历史缺失或水位落后时显示 uncertain
[ ] 人工订单和其他 EA/cBot 订单隔离
[ ] MT5 与 cTrader 相同输入得到相同风控决定
[ ] 账户个人访问权限、设备和 IP 符合 FTMO 要求
[ ] 禁止跨账户反向对冲、共享账号和第三方代交易
```

## 11. 不能自动化保证的事项

客户端 EA/cBot 不能替代服务器端订单权限。若账户仍允许人工终端或其他程序直接下单，系统只能做到监控和事后冻结，不能证明所有订单在下单前都被拦截。

禁止交易方法、账号共享、跨账户协调和第三方代交易不能仅靠本项目自动证明合规，必须执行 [合规矩阵](compliance-matrix.md) 中的组织控制。
