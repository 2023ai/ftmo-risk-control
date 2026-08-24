# FTMO 合规矩阵

核对日期：2026-08-23。

官方核对来源：

- [FTMO Challenge: 1-Step Trading Objectives](https://ftmo.com/en/trading-objectives/1-step/)
- [FTMO Challenge: 2-Step Trading Objectives](https://ftmo.com/en/trading-objectives/2-step/)
- [How long does it take to become an FTMO Trader?](https://ftmo.com/en/faq/how-long-does-it-take-to-become-an-ftmo-trader/)
- [I have successfully passed my FTMO Challenge. What’s next?](https://ftmo.com/en/faq/i-have-successfully-passed-what-to-do-now/)

规则仍以目标账户页面和当日生效条款为准；配置值不能替代上线时的账户级人工核对。

## 系统硬控制

| 规则 | 实现 |
|---|---|
| 1-Step 每日亏损 | 3% 官方线，2.4% 内部停止线 |
| 2-Step 每日亏损 | 5% 官方线，4% 内部停止线 |
| 最大亏损 | 10% 官方线，8% 内部停止线 |
| 1-Step 最大亏损追踪 | Prague 日界线的最高结算余额 |
| 以损定仓 | 本系统内部要求必须有止损，按平台货币风险向下取整；现有开放风险先扣减剩余缓冲，不等同于 FTMO 官方对止损的强制要求 |
| 新闻交易 | 本系统全阶段内部前后 10 分钟禁开；Standard FTMO Account 执行前后 2 分钟硬窗口 |
| 新闻持仓 | Standard 在 T-10 至 T-2 平仓撤单；硬窗口不再主动发送交易指令 |
| 长休市 gap trading | 所有阶段在持续至少 2 小时的休市前 120 分钟禁止新增风险 |
| 周末/长休市持仓 | Standard FTMO Account 在休市前 10 分钟平仓撤单 |
| 交易频率 | 5 分钟 3 笔、1 小时 10 笔、每天 30 笔 |
| 服务器请求 | 500 预警、1000 停止，低于 FTMO 异常活跃阈值 |
| 数据异常 | 账户超过 5 秒、新闻/市场日历超过配置阈值即 fail-closed；日界线使用服务器时间 |
| 触线记忆 | 内部 `LOCKED` 保持到下一 FTMO 日；官方 `BREACH` 跨日持久锁定 |
| 重启规避 | SQLite 保存账户日界线、开仓和请求频率 |
| 审计 | `request_id` 关联规则版本、决定和平台执行结果 |
| 日历持久化 | 新闻和休市快照、哈希、时间和规则版本写入 SQLite，重启恢复但不刷新年龄 |
| 备份恢复 | SQLite 在线一致性备份、`quick_check`、`0600`、停机锁和原子恢复 |
| 账户凭证 | 管理员先登记不可变账户基线；每账户最小作用域绑定、一次性明文、摘要存储、轮换重叠、过期和撤销 |
| 传输安全 | 默认 loopback；远程监听显式授权；支持直接 mTLS 或反向代理 mTLS |
| 可观测性 | Prometheus 请求、决定、日历过期状态、账户状态、不确定快照、未知执行、凭证和备份指标 |

## 独立资格目标

以下目标不会改变某一笔交易是否可以安全提交，因此不进入交易前闸门，而由独立资格看板判定：

- 1-Step Best Day Rule：按 Prague 日聚合当前阶段/周期已平仓净损益，计算 `best_day_profit / positive_days_profit`。
- 2-Step FTMO Challenge 和 Verification 的 Minimum Trading Days：按 Prague 日统计至少开过一个仓位的日期，跨日持仓不重复计数。
- 1-Step Evaluation、2-Step Evaluation 和 Verification 的 Profit Target。

资格记录使用 `account_id + phase + cycle_id` 隔离，防止把 Evaluation 利润带入 Verification，或把上一 Reward 周期带入下一周期。已平仓交易、开仓日和历史完整性水位缺一不可；缺失时返回 `uncertain`，不能显示正式达标。

上线时不得把本项目的 `ALLOW` 解释为“已满足全部 FTMO Trading Objectives”。同样，看板 `eligible=true` 只代表当前配置和完整同步历史的计算结果，最终状态仍以 FTMO 账户页面和公司合规复核为准。

## 组织控制

以下事项不能只依赖 EA/cBot：

- 每个账户只能由被授权的个人访问；禁止共享登录、第三方代交易和账号管理服务。
- 禁止跨多个 FTMO 账户协调同向/反向交易、规避风险限制或复制不属于本交易者的信号。
- 禁止延迟套利、错误报价利用、超高频服务器压力、模拟环境可行但真实市场不可复制的策略。
- 人工订单、其他 EA/cBot 和移动端订单必须被账户权限或交易网关隔离。
- 新闻和市场休市数据必须来自经过批准且与 FTMO 品种名称一致的数据源。
- FTMO 更新条款后，由合规负责人核对规则，更新配置并增加 `rule_version`。
- Prometheus 告警必须连接公司值班路由；仓库内规则文件本身不能证明通知已经送达。

## 上线结论

本项目可以硬拦截经过适配器的交易请求，并对已有持仓执行新闻/休市守护。只有在“所有下单路径均经过适配器或服务器网关”且组织控制已经落实时，才能将其作为公司强制风控系统。
