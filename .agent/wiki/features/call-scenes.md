# 调用场景 · LLM 调用点「选场景」策略组（设置页运行时面板）

> 前端主体 src/static/index.html（设置页 `/settings` 运行时 tab `#panel-runtime`；2026-11 f6d48cc 设置页页签化前为设置弹窗「运行时」页签）；场景解析消费端为引擎各 LLM 调用点。施工牌 p_ea32ad4f 引入；本页收录策略组语义与 2026-11-11 的 UI 修复轮（列表高度塌陷 + 「模型与回退」栏移除，用户实锤 + 裁定）。

## 职责：LLM 调用点从「选模型」改为「选场景」

所有 LLM 调用点不再直接填模型名，改为填**场景名**——模型怎么选（链、兜底）集中到设置页「调用场景」策略组一处配置：

| 消费端 | 填法 |
|---|---|
| sub-agent yml / main.yml 的 `model:` | 填场景名 |
| 工作流 llm 节点 / explore / img_proxy 的 model 字段 | 填场景名 |
| 补全 / utility 短调用 | `completer` / `short-req` 场景（原 `reasoning_completer` / `utility_model` 键的场景化接替） |

**解析语义**（策略组 title 原文口径）：

- 场景 miss → `default` 场景兜底——写错场景名不炸轮
- 场景内的链中卡片必须是「左侧已配置的模型名」——未配置的解析时**自动跳过**（不产生无效 provider）
- 设置面板的 `#scenList` 列表区逐行展示场景（含有效链展示，如 `glm-official-flash → deepseek`）

## 高度塌陷修复：#scenList 只配 max 无 min（2026-11-11，用户实锤）

**用户实锤**：调用场景的 UI 滑动区域高度是 0。

**根因**：`#scenList` 只配了 `max-height:32vh`（上限）没有下限——空态/加载态时内容为零，容器塌成 border 高度（约 9px），看起来就是高度 0。

**修复**（src/static/index.html，纯前端三处）：

| 改动 | 说明 |
|---|---|
| `#scenList` 加 `min-height:160px` | 列表区保底高度，空态/加载态也占位可见（`max-height:32vh;overflow-y:auto` 保留） |
| `#panel-runtime` 加 `overflow-y:auto` | 运行时页签整体可滚，内容再多不挤压 |
| JS 清理 | 「模型与回退」四键的回填/提交、`fillModelSelect` + `_cfgWanted` 机制整体退役（随栏移除，见下节） |

## 移除「模型与回退」栏（2026-11-11，用户裁定）

场景化之后，原「模型与回退」栏的四个配置入口冗余——**整栏撤下**（fallback_chain / fallback_policy / reasoning_completer / utility_model）。

**行为不变说明（关键）**——配置能力没有丢：

| 撤下的键 | 现在怎么配 | 旧行为 |
|---|---|---|
| `reasoning_completer` / `utility_model` | **completer / short-req 场景**接管（调用场景组新入口）；旧键仍从 settings.json 生效 | 键值行为不变 |
| `fallback_chain` / `fallback_policy` | 保留在 settings.json 生效（非 react 调用链，见 [回退链职责分离](../architecture/multi-agent.md#回退链职责分离react-只认-yml-声明设置页链只管非-react2026-09-15用户裁定)）；**UI 不再暴露**，走 `/config fallback_chain` 命令 | 键值行为不变 |

**保存不抹掉**：后端是**部分更新语义**（set_config 合并写，见 [set_config 保存语义修复](../guides/config-and-models.md#set_config-保存语义修复合并替代整体覆盖ui-保存不再抹掉非表单键随-v0300commit-54e7eda)）——保存其它设置不会抹掉这四个手改键（已实测 settings.json 四键幸存）。

**页签结构（移除后）**：运行时页签现为 **基础参数 → 调用场景 → NanoJev → 执行与上下文**。

## 保存入口并入运行时单按钮：双保存互覆盖根治（2026-11，commit 05ece4f）

**问题**：场景表此前在运行时 tab 有**独立保存按钮**，与下方运行时参数保存按钮并存——两者各自提交、**后保存的覆盖先保存的**（场景表的保存不含运行时参数、反之亦然，先存的一方改动丢失）。

**收口（commit 05ece4f）**：

- 双按钮合并为**单按钮「💾 保存运行时（含上方场景表）」**——场景表 + 运行时参数一次提交，不再互覆盖
- `scenarios` 键支持 **scope**——随「配置来源切换」（生效份 / 全局 / 本地）读/写对应份，与 models/settings 同语义
- 配套 settings 读写链路收口：src/config.py 改动态解析，消 import 时锚定的分裂

全貌见 [config-and-models · 设置页页签化与保存链路收口](../guides/config-and-models.md)。

## default 唯一兜底：未配置场景统一回退 default 链（2026-11-14，用户裁定，commits 76593f2 + 6e76d04）

**语义收口（76593f2）**：`resolve_scenario` 改**显式表判命中**——场景表只含用户显式配置过的场景（`load_scenarios(seed=False)`，内置场景不再播种进表）；命中按表取链，**未配置场景（含内置名）统一回退 default 链**，default 成为唯一兜底。此前 explore 等内置场景自带的**硬编码回退链退役**——内置名不再是有私有默认链的特殊场景，未配置 = default。配套：代看候选链过滤非视觉卡片（见 [image-input · 非视觉模型图片代理描述](image-input.md)）。

**API 可见形态（6e76d04，验证轮顺手补）**：`/api/scenarios` 的 `valid` 此前只按显式表生成——未配置场景连条目都没有，「未配置 → default 链」在 API 侧不可见（想验证「explore 显示 default 链」根本看不到）。现 `valid` 覆盖**显式表 ∪ 内置名全集**（内置名来自 `factory_scenarios()`，同时供「+ 添加场景」弹框选择）：未配置的内置场景直接给出它将拿到的链。设置页渲染按名字取 valid、多出的键无影响。

**实测（9000）**：

- 显式配置的 5 个场景 valid 全部正确（agent-work / agent-vision / short-req / completer / default）
- `explore`（未配置）→ `['glm-official','proxy','deepseek']` = default 链 ✓
- UI「+ 添加场景」选内置名 → 场景表新增行、描述自动带上（测试行未保存、settings.json 无变更）

**生效**：引擎侧 `/restart`（重启后 `valid.explore == valid.default` 即最终确认）。

## 验证（playwright 实测 9000）

- 场景列表高度 **160px**、6 个场景行正常渲染（含有效链展示）
- 「模型与回退」组消失；保存路径不炸、console 0 error
- settings.json 四键幸存（local-lfm-vl / utility / fallback_chain / fallback_policy 均在）

**生效**：纯前端改动，**Ctrl+F5 刷新即生效**（无需 `/restart`）。

## 注意事项

- 场景名写错不炸轮（miss → default 兜底），但配置等于没生效——排障先核对场景名与列表展示的一致性
- 场景链中的模型名必须与「左侧已配置的模型卡片」一致，未配置的自动跳过——新加模型先建卡片再挂进链
- 手改 settings.json 的 fallback_chain / fallback_policy / utility_model / reasoning_completer 仍是有效配置源（UI 移除 ≠ 键退役）；想改回退链用 `/config fallback_chain`

## 相关页面

- [配置体系与模型调优](../guides/config-and-models.md) —— settings.json 键表（fallback_chain / utility_model 等键的运行时语义）
- [多 Agent 体系 · 回退链职责分离](../architecture/multi-agent.md#回退链职责分离react-只认-yml-声明设置页链只管非-react2026-09-15用户裁定) —— fallback_chain 的「非 react 调用」作用域
- [Agent 管理页](../features/agents-admin.md) —— react 主调用回退链的 .yml 声明入口（fallback 表单）
