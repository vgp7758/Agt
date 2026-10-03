# 项目推广与冷启动 · awesome-openrouter PR 复盘 + 渠道策略（2026-10-03）

> 背景：Agt 是 pip 包（`agt-agent`）+ 开源 repo（github.com/vgp7758/Agt），推广处于冷启动期。本页记录对外推广动作、外部反馈与渠道决策，供后续重提/新渠道动作参考。

## 事件：awesome-openrouter PR #132 被关（2026-10-03）

提交 [OpenRouterTeam/awesome-openrouter#132](https://github.com/OpenRouterTeam/awesome-openrouter/pull/132)「Add Agt — self-building agent framework with built-in OpenRouter provider」，维护者 kenrogers 关闭，两个理由与实情：

| 关闭理由 | 实情 | 定性 |
|---|---|---|
| **no evidence of traction or usage yet** | star 还少——awesome 列表的收录标准就是社区热度 | 硬事实，无法速成，只能攒 |
| **docs link is invalid** | 实锤——`docs/` 目录没有入口文件（只有 architecture/01-06 等八个散文件，无 README.md/index.md），PR 表格里的入口链接 404 | 当日已修 |

## 当日修复（两处）

1. **新建 `docs/README.md` 索引页**：六篇架构文档导航 + workflow-spec / external-injection 规范 + 上手指引 + 时效说明——docs 链接从此有效，根治「docs link invalid」。此前 docs/ 有内容无入口，任何外链进不来
2. **README 反馈章节诚实化**：撤掉过时承诺「提交即直达作者手机（飞书实时推送）」——随包 `DEFAULT_WEBHOOK_URL` 已于 v0.31.2 撤销、宣传不成立；GitHub Issues 置顶为推荐通道，作者联系方式明列（详见 [feedback · 随包 webhook 撤销](../features/feedback.md#随包-webhook-撤销v0312--readme-反馈章节诚实化2026-10-03)）

## 渠道判断与策略

- **awesome 列表是热度认证，不是冷启动渠道**——真正的冷启动杠杆：知乎 blog 系列（深度技术文 = 长尾搜索流量，06 篇待发）+ PyPI 版本迭代节奏（release 历史本身就是活跃度证据）
- **现在不重提 PR**：同样理由会再被关一次，且连续被拒的记录比「暂未提交」更难看。正确姿势三步：
  1. 在 PR #132 下礼貌回复（致谢 + 反馈已采纳 + 择机再来），留「反馈会听」的好印象
  2. 攒 traction：知乎 blog 系列发完 + PyPI 下载量随版本积累
  3. 重提时机：**star ≈ 50+** 时提新 PR（链接有效 + 有真实使用者背书）

## PR #132 回复文案（留存备用）

> Thanks for taking a look @kenrogers! Fair points — the docs link pointed to a non-existent path; I've added a proper index at https://github.com/vgp7758/Agt/blob/main/docs/README.md. Understood on traction — we'll revisit once there's more organic usage. Appreciate you maintaining this list 🙏

（由用户手动贴到 PR，Agent 不代发）

## 相关页面

- [feedback 反馈通道](../features/feedback.md)：随包 webhook 撤销与 README 反馈章节改版
- [home](../home.md)：快速事实总览
