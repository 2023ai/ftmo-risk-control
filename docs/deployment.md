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

`/health` 是存活信息，可在日历尚未就绪时返回 `200`。完成 SQLite 和两种日历同步后，再用同一管理员令牌请求 `/ready`；只有返回 `200` 且 `ready_for_risk_increase=true` 时才允许平台开始新增风险。

非 loopback 监听默认被拒绝。确需从另一台机器访问时，必须显式增加 `--allow-remote-bind`，并放在专用内网、VPN 或已认证的 TLS 反向代理后面，同时限制来源 IP；不能直接把明文 HTTP 服务暴露到公网。

SQLite 状态文件和 JSONL 审计文件会被服务收紧为仅属主可读写（`0600`）。部署目录仍应由专用系统账户持有，并禁止其他用户读取。

## 2. 账户凭证

每个账户在平台接入前，必须由管理员用已核对的实盘/模拟余额完成首次登记。首次登记固定账户类型、阶段、风格、初始资金和日界线基线；平台凭证不能创建账户或修改这些静态字段：

```bash
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"account_id\": \"mt5-10001\",
    \"account_type\": \"two_step\",
    \"phase\": \"evaluation\",
    \"style\": \"standard\",
    \"initial_capital\": \"100000\",
    \"day_start_balance\": \"100000\",
    \"highest_settled_balance\": \"100000\",
    \"balance\": \"100000\",
    \"equity\": \"100000\",
    \"current_open_risk\": \"0\",
    \"open_positions_count\": 0,
    \"pending_orders_count\": 0,
    \"as_of\": \"$NOW\"
  }" \
  http://127.0.0.1:8765/v1/account-sync
```

然后签发供 MT5/cTrader 使用的独立平台凭证：

```bash
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "expires_at": "2026-09-22T12:00:00+00:00"
  }' \
  http://127.0.0.1:8765/v1/admin/accounts/mt5-10001/credentials
```

把响应中的一次性 `secret` 放入目标平台的 `AccountCredential`。禁止把它写进仓库、截图、工单或共享密码库以外的位置。默认凭证只含 `account:sync`、`trade:evaluate`、`trade:execution` 和 `calendar:read`，不能确认日结算或写入资格历史。

轮换：

```bash
curl -sS \
  -H "X-Risk-Token: $RISK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"overlap_seconds":300}' \
  http://127.0.0.1:8765/v1/admin/credentials/CREDENTIAL_ID/rotate
```

先把新秘密部署到平台，确认账户同步成功，再等待重叠窗口结束。紧急撤销调用 `/v1/admin/credentials/{credential_id}/revoke`。列表接口不返回秘密。

日结算同步器必须显式申请 `{"scopes":["account:settlement"]}`；资格导入器必须显式申请 `{"scopes":["qualification:write"]}`，账户级资格读取使用 `qualification:read`。默认凭证 TTL 为 30 天，最大 90 天，轮换重叠 5 分钟；均由 `config/ftmo-v2.json` 控制。Prometheus 应对即将过期凭证另接公司的证书/密钥管理系统；本服务不会发送含凭证信息的通知。

旧库中若遗留 `admin:*` 账户凭证，服务会拒绝其授权；必须重新签发显式最小作用域凭证，不能把管理员能力迁移到平台适配器。

升级已有 SQLite 时服务会自动迁移新增字段和表。V5 日历必须含覆盖起止时间，且 `rule_version` 已更新；升级后必须重新同步新闻和休市日历，不能继续使用旧快照开放新增风险。升级前已经发生但当前权益已恢复的历史触线无法由快照重建，首次上线该版本前必须人工核对账户历史和 FTMO 状态。

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

`config/news-events.example.json` 只是格式示例，不是实际新闻日历。示例用 `*` 保守阻断全部品种，防止未经审核的局部映射漏掉受影响资产。上线前应由公司人员或已批准的数据源生成并审核 `affected_symbols`、`coverage_start` 和 `coverage_end`；只有完整覆盖 FTMO 公布的受影响货币、指数和其他资产及经纪商品种后缀后，才能把 `*` 缩小为受控列表。经纪商带后缀的品种可使用尾部通配符，例如 `EURUSD*`。

```bash
python3 scripts/sync_news.py \
  --file config/news-events.example.json \
  --url http://127.0.0.1:8765
```

新增风险请求必须有不超过配置阈值（默认 60 秒）的新鲜新闻数据，且覆盖范围必须包含当前时间前后的全部内部新闻窗口。同步脚本只刷新 `fetched_at`，不会自动延长审核文件的覆盖时间。同步成功后 SQLite 保存快照，API 重启会重算内容哈希再恢复；恢复不改变原 `fetched_at`，因此过期或覆盖不足的快照仍会 fail-closed。若持久化快照的 `rule_version` 与当前配置不一致，服务拒绝加载并要求重新同步。

周末和超过 2 小时的休市日历也必须由批准的数据源生成：

```bash
python3 scripts/sync_market.py \
  --file config/market-closures.example.json \
  --url http://127.0.0.1:8765
```

市场日历超过 `market_close_controls.max_schedule_age_seconds`（默认 1 小时）未更新，或 `coverage_end` 没有延伸到未来 120 分钟禁开仓窗口之外时，新增风险保持 fail-closed 并发出告警。

所有 POST 请求都会返回 `request_id`。设置 `RISK_AUDIT_PATH` 后，服务以 JSON Lines 格式记录请求 ID、规则版本、决定、账户状态、HTTP 状态和错误原因。

日界线前后必须由批准的数据源调用 `/v1/settlement-sync`。如果没有确认的结算余额，账户会进入 `data_uncertain`，只允许降低风险的动作。

内部 `LOCKED` 当日不可自动解除；官方 `BREACH` 不提供在线清除接口。误报或数据修复必须停止服务、保留审计证据并由授权人员处理，不能通过重启 EA/cBot 或 API 绕过。

平台、API 主机和结算数据源都必须启用可靠的 UTC 时间同步。客户端时间只接受 ±30 秒偏差，日界线、新闻、休市、频率和执行活动使用服务器接收时间判定。

服务端也会持久化未知执行结果。出现 `REJECT_UNKNOWN_EXECUTION` 后，只允许关闭、减仓、修改为降低风险和撤销挂单；必须用原 `request_id` 补报最终 `success`/`failure`。若最终结果无法确认，锁必须保留，不能通过重启服务绕过。

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
  --source /var/backups/ftmo-risk/risk-state-2026-08-26.db \
  --state "$RISK_STATE_PATH"
systemctl start ftmo-risk-api
```

运行中的 API 会持有 `.server.lock`，恢复工具检测到锁时拒绝覆盖。恢复后检查 `/health`、日历时间与覆盖范围、账户状态、未知执行和资格历史，最后确认 `/ready` 返回 `200` 再开放平台连接。

## 6. Prometheus 与告警

将 `monitoring/prometheus.example.yml` 中的管理员令牌文件路径替换为受保护文件，并加载 `monitoring/alerts.yml`。令牌文件必须 `0600`，Prometheus 使用 Bearer 头抓取 `/metrics`。

最低告警闭环：

- 数据库不可用：立即停止新增风险并升级为 critical；
- 新闻/休市缺失、过期或覆盖不足：保持 fail-closed，检查同步器和审核文件；
- 未知执行：人工核对订单和频率预留；
- 凭证临近过期：在 24 小时告警窗口内完成轮换并验证同步；
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

`uncertain` 账户不得用于正式资格判断。Profit Target 还要求账户余额达到目标并确认 `open_positions_count == 0`、`pending_orders_count == 0`；缺少任一库存或账户快照超过资格新鲜度阈值时显示需复核。看板不会改变交易闸门；普通交易是否 `ALLOW` 与资格是否达标相互独立。

## 8. MT5 接入

- 在 MT5 的工具设置中把风险 API 地址加入 WebRequest 白名单。
- 把 `RiskGuardEA.mq5` 放入 `MQL5/Experts`，用 MetaEditor 编译。
- 仓库只发布 `platform/mt5/RiskGuardEA.mq5` 源码；请在目标 MetaTrader 5 环境中用 MetaEditor 编译，不要把本地 `.ex5` 或 `.log` 文件提交到仓库。
- 策略只能调用 `RiskGuardBuy()`、`RiskGuardSell()`、`RiskGuardClose()`、`RiskGuardClosePartial()`、`RiskGuardModifyPosition()` 和 `RiskGuardCancelPending()`，不得直接调用 `CTrade`。
- Bootstrap 两个余额参数必须与第 2 节的管理员首次登记一致；平台凭证只能同步已登记账户，之后由 SQLite 状态层按 Prague 日界线维护。
- 每次同步必须回传账户级 `open_positions_count` 和 `pending_orders_count`；它们用于资格判定“账户已清仓且无挂单”，不能只用当前策略的持仓数代替账户库存。
- `AccountCredential` 只能使用当前 MT5 账户对应的秘密，不能复用管理员令牌或其他账户凭证。
- 交易量、点值、佣金、Swap 和合约货币必须用真实品种规格回放验证。
- 已有持仓风险必须用当前 Bid/Ask 到止损计算，挂单风险使用目标入场价到止损计算。
- 修改止损的新增风险也必须以当前可平仓报价到新旧止损计算；禁止通过移除止损把风险标记为减少。
- 适配器时间戳必须带毫秒精度或单调递增；同一时间戳上传冲突快照会被服务拒绝。

## 9. cTrader 接入

- 将 `RiskGuardBot.cs` 放入 cTrader Automate 项目并编译。
- 在启动 cBot 前，先按第 2 节由管理员登记账户基线；cBot 的 `Bootstrap Day Balance` 和 `Bootstrap High Balance` 必须与该登记一致。
- 策略只能调用 `TryExecuteBuy()`、`TryExecuteSell()`、`TryClose()`、`TryClosePartial()`、`TryModifyPosition()` 和 `TryCancelPendingOrder()`。
- 使用当前账户的实际品种规格验证 `AmountRisked()`、交易量单位和最小步进。
- 对平仓、修改止损、取消挂单也接入相同的 API 检查和审计流程。
- 新闻与休市状态分别判断；一个状态接口失败时，另一个已确认的平仓或撤单信号仍必须执行，同时对缺失状态告警。
- `AccountCredential` 只能使用当前 cTrader 账户对应的秘密。
- 账户同步必须回传账户级 `Positions.Count` 和 `PendingOrders.Count`，不能只统计当前图表品种。

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
[ ] 新闻同步超过配置的 `news_controls.max_calendar_age_seconds`
[ ] 周末/超过 2 小时休市前 10 分钟平仓和撤单
[ ] 市场休市日历超过配置的 `market_close_controls.max_schedule_age_seconds`
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
