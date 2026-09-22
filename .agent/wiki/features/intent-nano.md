# intent_nano · 判别式意图路由节点（内置 Intent 的加强版，NanoJev 决策模型）

> 2026-09-22（commit `2aabfaa`）。节点插件 type `intent_nano`（🧭 Intent·Nano，category llm）：用本地 NanoJev 0.6B 决策模型做**判别式**意图分类，替代内置 Intent(22)「LLM 生成式 + 编号解析」路线。背景：用户先在 8090 的 `D:\Programs\env\lfm_proxy.py` 服务侧加好了 nanojav 决策模型，本轮把该能力设计实现成工作流节点插件并端到端调试通过。

## 职责：与内置 intent(22) 的对比

| 维度 | 内置 Intent（type 22，生成式） | intent_nano（判别式） |
|---|---|---|
| 判定方式 | 提示词 → LLM 回复编号 → 字符串解析 | NanoJev（Qwen3-0.6B backbone + 决策头）**一次前向直接输出候选概率分布** |
| 速度/成本 | 一次 LLM 调用，token 计费 | CPU fp32 单次前向 ~1s，**零 token 费用** |
| 歧义 | 编号解析可能失败/串号 | 无——直接出 choice |
| 可解释 | 无 | 完整 `probabilities` 分布 + `confidence` |
| 软拒识 | 无 | **top1 置信度 < threshold → 走 default**（不确定样本不硬路由） |
| 意图描述 | 只用 name 拼提示词 | **XML 描述体直供模型**作 criteria 语义描述 |

## 服务链路

2026-09-22 通用化（用户提案）：intent_nano 从「本机 NanoJev 专属」升级为**通用常用节点**——用户可在设置页配自己的 Jev 兼容服务，未配时三级降级保证任何环境都跑得通。

```
① settings.jev_base_url（自建 Jev 兼容服务，配 jev_api_token → Authorization: Bearer）
        ↓ 未配置
② 本机 8766 默认（lfm_services.json 的 nanojev，本机装了 NanoJev 才自动拉起 ≤60s）
        ↓ 也不可用（其它机器的自建/未装场景）
③ LLM 生成式降级（同内置 Intent(22) 提示词；conf=-1 标记降级模式）
   —— 任何环境都跑得通，不炸工作流
```

- **远程/自建 Jev**（settings 配了 `jev_base_url`）：直连该地址，**不自动拉起**本机服务（远程环境拉本机进程无意义）
- **本机默认**（未配 `jev_base_url`）：本机路径存在时自动拉起 nanojev_server（detached + 等 health 最长 60s）
- **LLM 降级**（两级都不通）：走生成式分类（同内置 Intent(22) 提示词），outputs 标记 `confidence=-1` 区分降级模式——**任何环境都可用，不炸工作流**

## 设置项（settings.json）

2026-09-22 通用化新增——WebUI「⚙ 设置 → 运行时」面板（用户可填自己的 Jev 服务）：

| 设置键 | 说明 |
|---|---|
| `jev_base_url` | Jev 兼容服务地址（如 `http://your-jev:8766`）；留空 = 先试本机 8766，再降级 LLM 生成式 |
| `jev_api_token` | 自建 Jev 服务鉴权 token（请求头 `Authorization: Bearer`）；服务无鉴权可留空 |

- **保存**走既有 `set_config` 通道写 `settings.json`（scope 切换同样适用——见 [config-and-models · 设置页配置来源切换](../guides/config-and-models.md#设置页配置来源切换生效份--全局--本地2026-08-31commit-ad0f385)）；前端 `fillSettingsForm` / `saveSettings` 已贯通
- **后端读取**：`config.load_jev_config()`（src/config.py）——返回 `{base_url, api_token}` dict，未配置时 base_url 为空字符串、字段缺失兜底空值
- **消费端**：`_jev_target()`（intent_nano.py）——有 base_url → 直连远程（Bearer 鉴权，**不拉起本机**）；无 base_url → 本机 8766 默认 + 本机路径存在时允许拉起

## 四件套实现

| 文件 | 内容 |
|---|---|
| `src/assets/nodes_builtin/intent_nano.py` | handler：SDK 解析输入 → HTTP 直调 8766 → outputs `intent`/`confidence`/`probabilities`；出口 branch_N / default |
| `src/assets/nodes_builtin/intent_nano.js` | 编辑器：intents 双列编辑（name + 语义描述，description 直供模型判别）+ temperature/threshold 数字控件 + 画布摘要行 |
| `src/workflow_xml.py` | 读/写两侧 type 分支 `"22"` → `("22", "intent_nano")`：`<intent name="提问">用户想了解…</intent>` 描述体往返存活（内置 22 的提示词同样可用描述体）；内置 22 旧档自闭合无体 → 空 description，向后兼容 |
| `src/static/workflow_editor.html` | 出口端口生成 + 画布摘要分支条件并入 intent_nano——branch_N/default 与内置 22 **同构** |

**同构的直接红利**：存量工作流把 Intent(22) 节点换成 intent_nano 即完成迁移，下游 selector 连线零改动。

## XML 用法示例

```xml
<node id="160001" type="intent_nano" title="意图路由">
  <in name="query" ref="100001.user_message"/>
  <in name="threshold" type="number">0.35</in>
  <intent name="code">要求编写、修改、调试代码</intent>
  <intent name="search">要求检索资料、查文档</intent>
  <intent name="chat">闲聊或常识问答</intent>
</node>
<!-- 出口 branch_0/branch_1/branch_2 + default，下游 selector/text 随意接 -->
```

## 调试验证链（全绿）

**首版三链（2026-09-22，commit 2aabfaa）**：
1. **nanojev 原生接口实测**：choice + probabilities 分布 ✓
2. **JS 语法 / XML 往返幂等**：描述体保留、无描述自闭合，二次转换逐字节一致 ✓
3. **端到端**（子进程跑新代码）：「帮我写一个快速排序的 python 函数」→ intent=`code`（confidence 0.536）→ branch_0 扇出 → text 分支执行 → end 输出 ✓

**通用化四链（2026-09-22 二轮）**：
4. **JS 语法 1/1** ✓
5. **主链**（未配 jev）→ 本机 NanoJev 判定 ✓
6. **降级链**（假远程 jev base_url）→ **不拉起**本机服务 → LLM 降级 conf=-1 → default ✓
7. **未配 → 本机默认 8766 + 允许拉起** ✓；XML 往返 / 端口扇出（前轮已验）✓

调试中抓到的坑：**`/reload nodes` 只热载节点插件（.py/.js），不含 `workflow_xml.py`**——旧进程解析器不认识 intent_nano，`debug_workflow` 显示空参（现象像「插件没生效」，实为旧 XML 解析器在跑）；`/restart` 后即正常。测试工作流保留在 `.agent/workflows/_nano_test.xml`，可当模板抄。

## 注意事项

- threshold 建议 0.35 起调：调低会硬路由歧义样本，调高则大量走 default
- intent name 是路由端口（branch_N 按 intents 数组序号生成），description 才是判别依据——想让模型判得准，描述体写清楚语义边界
- 内置 22 的提示词也可用描述体（同一解析分支向后兼容），旧档不写体零影响
- **降级模式**（2026-09-22 通用化）：未配 `jev_base_url` 且本机无 NanoJev 时自动走 LLM 生成式分类，outputs 标记 `confidence=-1`——下游 selector 可据此区分「判别式判定」与「降级生成式」；降级模式下 threshold 不生效（生成式无概率分布）

## 相关页面

- [节点插件化](../architecture/node-plugins.md) — 插件目录约定 / EdFW 组件 / 热加载 / 本节点即「第四批」
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 13 类节点速查（内置 intent(22) 语义）
- [本地模型](../guides/local-models.md) — NanoJev 决策模型画像与托管形态
- [工作流编辑器 UX](editor-ux-improvements.md) — EdFW 组件渲染细节
