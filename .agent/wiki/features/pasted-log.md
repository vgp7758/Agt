# 大段粘贴标记 &lt;pasted-log&gt; · WebUI 粘贴自动包裹 + extract_keywords 提词剥离

> src/static/index.html（前端标记）+ extract_keywords 子工作流（strip_pasted 剥离），2026-09-13，commit `f1b4ecb`，用户提案。

## 职责

「贴大段日志问问题」是高频姿势，但大段日志对**关键词提取**是纯噪声——本地小模型对日志里的技术词（pip / OSError / idna 这类包名报错词）极敏感，提取出一串与用户真实问题无关的关键词，导致 wiki / 记忆检索误命中。方案两端配合：

- **前端标记**：往输入框粘贴大段文本时自动包裹 `<pasted-log>...</pasted-log>`——显式、可见、可手删；
- **工作流剥离**：extract_keywords 链头剥掉这种块，只用包裹外的正文提词。

**user_message 原文照常归档投影**——主 Agent 看得到完整日志，只有关键词提取忽略它。归档 / 上下文引擎零改动。

## 前端：粘贴即标记（src/static/index.html）

document 级 `paste` 监听（与图片粘贴 addImageFile 共用同一监听器）：

- **触发条件**：粘贴目标是输入框（`e.target === inp`）且文本 ≥6 行或 ≥400 字符（典型日志形态；阈值刻意保守——正常打消息、贴一两行命令不触发）
- **动作**：文本自动包裹 `<pasted-log>…</pasted-log>` + toast 提示：

  > 📋 大段粘贴已标记 `<pasted-log>`（23 行）——关键词提取将忽略这段

- 标记就是普通文本，**可见可手删**

**生效**：WebUI **Ctrl+F5** 强刷（纯前端监听，后端零改动）。

## 工作流：链头 strip_pasted 剥离（.agent/workflows/extract_keywords.xml）

链头插入 strip_pasted 节点：`100001 → 115001(strip_pasted) → claim`，正则剥掉 `<pasted-log>…</pasted-log>` 块，之后**剥离后文本**贯穿四处、全部同源：

| 消费点 | 说明 |
|---|---|
| kv_cache_claim 的 key | 缓存键口径 = 剥离后文本（[kv-tools](kv-tools.md)） |
| LLM 节点的 q | 提词输入 |
| kv_cache_write 的 key | 写回与读取同键 |
| 等待循环 recheck 的 key | pending 轮询同键 |

四处同源 → 缓存 hit / pending / claim 口径不会错位。工作流按需读盘——**下一轮 before_turn 即生效**，无需 /restart。

## claim 等待循环修复 + 播种源三文件对齐（2026-09-17，commits 553d2cd + 5992929）

### 553d2cd：等待循环就绪判定改自包含

extract_keywords 的 claim 等待循环此前有个跨轮漂移判据：`ready?` 条件2 引用**上一轮的 `keywords`**（循环外变量）——pending 轮判据过期，且 pending 轮还会对 `keywords` 脏写。修复（commit 553d2cd）：

- `ready?` 条件2 改引用**本轮 `recheck.hit`**——拿到值当轮即 break，判定自包含于本轮
- 删 pending 轮对 `keywords` 的脏写

工作流 XML 按需读盘，`.agent/workflows/` 改完下一轮 before_turn 即生效。

### 5992929：播种源对账——src/workflows/ 三个文件落后

全量对账（`diff -rq .agent/workflows/ src/workflows/`）发现随包播种源 `src/workflows/` 三个文件落后于运行版，一并 cp 对齐 + filecmp 逐字节验证：

| 文件 | 播种源落后内容 |
|---|---|
| `extract_keywords.xml` | 还是旧 read→write-pending 模式（双 miss 窗口）——缺 claim 原子互斥版与上述 553d2cd 修复 |
| `recap_gen.xml` | 运行版迭代未回播种源 |
| `wiki_auto_maintenance.xml` | 同上 |

无「仅播种源有」的孤儿文件。

> **双层漂移通用教训**：运行版（`.agent/workflows/`）与播种源（`src/workflows/`）是两份独立文件，运行版迭代**不会自动回写**播种源——与工具层 workspace/assets 双层对账（见 [工具外置 · 双层一致性对账](tool-externalization.md)）同款问题；对账要 `diff -rq` 穷尽，不能凭上次同步的记忆。

### 补丁分发（patches/ 累计 4 个）

两笔 commit 已导出补丁（清单见 [user-interaction · patches/ 导出](user-interaction.md)）：

- `patches/0001-fix-workflow-extract_keywords-claim-pending-recheck.patch`（553d2cd）——修的是**目标环境实际在跑的** `.agent/workflows/`，git am 后热加载即生效、无需重启
- `patches/0002-chore-workflows-extract_keywords-claim-recap_gen-wik.patch`（5992929）——播种源对齐；已部署环境通常不自动重播种已有文件，播种源更新也靠补丁；全新安装则直接从新播种源起步

## 实测（本地 lfm 真跑）

| 输入 | LLM 实际收到 | 提取结果 |
|---|---|---|
| `帮我看看这个报错是怎么回事 <pasted-log>pip…OSError…</pasted-log>` | 只有 `帮我看看这个报错是怎么回事` | 未被 pip/OSError/idna 带偏 ✓ |
| 正常消息（回归） | 原文 | `构建流水线\|单元测试\|并发\|超时\|资源争用` ✓ |

## 已知边界

- 整条消息**只有日志没有任何文字** → strip 后为空——小模型会对空输入回一句「请提供报错信息」之类（clean 后成一个噪声词，无害）。若常这么用，可加「strip 后为空直接返回空列表」短路。
- 标记是**内容协议**而非粘贴专属——手动敲的 `<pasted-log>` 块同样会被剥离（合理：手敲这种标记=主动声明噪声段）。

## 与其他模块的关系

- **extract_keywords 子工作流**：wiki_auto_query / before_turn_retrieval 两个 before_turn 钩子工作流各自内嵌调用——两路检索同时受益（[wiki_auto_query](wiki-auto-query.md)、[长期记忆](longterm-memory.md)）
- **kv_cache 三件套**：claim / write 键口径变为剥离后文本（消费方传入，[kv-tools](kv-tools.md)）
- **WebUI 输入框**：与图片粘贴同一 paste 监听器（[user-interaction](user-interaction.md)）

## 相关页面

- [wiki_auto_query](wiki-auto-query.md) —— extract_keywords 所在检索链路（v4 流水线）
- [kv_cache 三件套](kv-tools.md) —— claim 原子互斥与缓存键口径
- [长期记忆](longterm-memory.md) —— 另一路 before_turn 检索（同样走 extract_keywords 提词）
- [用户交互](user-interaction.md) —— WebUI 输入框 / 粘贴事件所在前端
