# 长期记忆 · 三类记忆 × episodic 召回三代演进

> 系统叙事完整版见博客第 4 篇《Agent 的长期记忆不是全塞进 prompt：三类记忆 × 三代召回演进》（2026-08-21 扩写完成，~2500 → ~4000 字，结构：问题 → 三类框架 → 最难一类深度演进 → 写入侧设计 → 存储 → 边界 → 收尾）。本页沉淀其中的技术事实。检索流水线背景见 [wiki_auto_query](wiki-auto-query.md)（博客第 3 篇，两篇互相引用成系列）。

## 职责

跨 session 记住用户偏好、项目事实、程序经验与会话历史，**检索命中才注入**——不是全塞进 prompt。每轮 before_turn 自动检索历史记忆并注入相关条目（实测：问「上一篇博客要补充什么」，自动召回并引用一周前的原话——这条活例已写进博客开篇）。

## 三类记忆

| 类 | 内容 | 注入形态 |
|----|------|---------|
| 事实 | 用户背景、主力模型、默认配置等 | 始终生效，每轮常驻 |
| 程序经验 `pro_*` | 踩坑教训（如「replace_lines 的 entries 必须传数组对象」） | 仅标题常驻，详情按需 `read_procedure(id)` |
| episodic | 历史 session 的对话/决策片段 | 检索命中才注入（**最难的一类，见下**） |

## episodic 召回三代演进（博客第 4 篇最重量级章节）

| 代 | 方案 | 问题 / 突破 |
|----|------|------------|
| 第一代 | 标点分词 + 子串匹配 | 中文整句一个 token，命中率靠缘分 |
| 第二代 | 本地 3B 提关键词 | **「量力分工」**：3B 做不了相关性判断（打分区分度差）但抽词绰绰有余 |
| 第三代 | 并入统一检索流水线 | **「该不该注入」从检索层上移到精排层**：召回管高召回、精排管高精度 |

- 第二代的「量力分工」洞察与 [wiki_auto_query v4](wiki-auto-query.md) 的核心结论同源：本地 3B 相关性打分区分度差（相关 0.1 / 无关 0.2 / 全 0.5），不如 embedding 余弦（0.69/0.42/0.16）；小模型只该干提词的活。
- 第三代即复用第 3 篇的检索工作流架构（提关键词 → 搜索 → cosine 精排 → 阈值裁决），与第 3 篇形成系列呼应。检索型钩子「选择+摘录、禁止生成」的输出纪律同样适用（见 [workflow-hooks](../architecture/workflow-hooks.md#检索型钩子的输出纪律选择摘录禁止生成)）。
- 旧版 `episodic_block` 直调示例已从博客删除——三代演进讲完后已过时。

## 写入侧的两个设计

1. **防重复沉淀（幂等写入）**：同一教训不得重复入库。真实糗事：replace_lines 的 entries 参数教训被记了两次——博客以此为反面案例。
2. **双主权管理**：`/memory` 页面供用户查看/编辑记忆——「Agent 记它的，用户管着」。没有用户主权，记忆库会退化成「Agent 的偏见库」。

## 存储

- 存档路径：`~/.agt/repos/<repo 目录名>/memories/`（与 sessions/plans/specs/images/rag 同级，见 [guides/ops · 存档布局](../guides/ops.md#存档布局agtrepos)）。
- **工程决策：hash → 可读转写**。早期路径用 `<workspace_hash>`，对着 `18f8db495cec` 这样的目录名无法知道是哪个项目——改为 repo 目录名可读转写。这条修正本身作为小决策点写进了博客。
- 记忆操作工具五件套（含 `read_procedure` 等读侧工具，博客已补全清单）。

## 注意事项

- 程序经验只常驻标题是刻意的 token 经济设计——详情按需读取，避免记忆本身膨胀上下文。
- episodic 注入与否由精排层阈值裁决，与 [wiki_auto_query](wiki-auto-query.md) 的 top1<0.5 不注入同一原则：召回宁滥勿缺，注入宁缺勿滥。

## 相关页面

- [wiki_auto_query](wiki-auto-query.md)：第 3 篇检索工作流，第三代 episodic 召回的架构来源（两篇博客互引）
- [workflow-hooks](../architecture/workflow-hooks.md)：before_turn 钩子约定与检索型钩子输出纪律
- [guides/ops](../guides/ops.md)：存档布局（memories 目录）
- [context-engine](../architecture/context-engine.md)：注入后落在投影的哪个位置
