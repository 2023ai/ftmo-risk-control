# 贡献指南

## 开始前

请先阅读 `README.md`、`docs/architecture.md` 和 `SECURITY.md`。提交内容必须保持 fail-closed 风控边界，不得通过客户端输入绕过日亏、最大亏损、新闻时段、交易频率或未知执行结果锁。

## 开发要求

- 使用 Python 标准库和仓库已有模式，避免无必要的依赖。
- 不提交真实账户数据、token、日志、SQLite 数据库、`.env` 文件或平台编译产物。
- 规则变化必须更新 `rule_version`、相关文档和测试；不能静默改变历史审计含义。
- 平台适配器的新增交易入口必须经过统一风控 API，并覆盖明确失败和未知结果。

## 提交前检查

```bash
python3 -m pip install -r requirements-dev.txt
make test
make check
make lint
```

如果修改 MT5 或 cTrader 适配器，还必须在对应平台编译源码，并说明编译版本和结果。不要连接真实 FTMO 账户或发送真实订单作为 CI 验证步骤。

## Pull Request

Pull Request 请说明：

- 修改的风险规则或接口；
- 对 `ALLOW`、拒绝、异常和恢复行为的影响；
- 新增或更新的测试；
- 是否需要重新进行 MT5/cTrader 编译验证。
