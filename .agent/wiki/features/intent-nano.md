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

```
nanojev_server（http-task 型）
  127.0.0.1:8766 · lfm_services.json 注册 · lfm_proxy --managed 托管（D:\Programs\env\lfm_proxy.py @ 8090）
        ↑ 插件直连（省一层 8090 转发）
intent_nano 节点 handler
```

- 8766 不可达时 handler **自动拉起** nanojev_server（detached + 等 health 最长 60s）
- 仍失败/超时 → `intent=""` 走 default 分支——**服务挂了不炸工作流**

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

1. **nanojev 原生接口实测**：choice + probabilities 分布 ✓
2. **JS 语法 / XML 往返幂等**：描述体保留、无描述自闭合，二次转换逐字节一致 ✓
3. **端到端**（子进程跑新代码）：「帮我写一个快速排序的 python 函数」→ intent=`code`（confidence 0.536）→ branch_0 扇出 → text 分支执行 → end 输出 ✓

调试中抓到的坑：**`/reload nodes` 只热载节点插件（.py/.js），不含 `workflow_xml.py`**——旧进程解析器不认识 intent_nano，`debug_workflow` 显示空参（现象像「插件没生效」，实为旧 XML 解析器在跑）；`/restart` 后即正常。测试工作流保留在 `.agent/workflows/_nano_test.xml`，可当模板抄。

## 注意事项

- threshold 建议 0.35 起调：调低会硬路由歧义样本，调高则大量走 default
- intent name 是路由端口（branch_N 按 intents 数组序号生成），description 才是判别依据——想让模型判得准，描述体写清楚语义边界
- 内置 22 的提示词也可用描述体（同一解析分支向后兼容），旧档不写体零影响

## 相关页面

- [节点插件化](../architecture/node-plugins.md) — 插件目录约定 / EdFW 组件 / 热加载 / 本节点即「第四批」
- [工作流引擎与钩子](../architecture/workflow-hooks.md) — 13 类节点速查（内置 intent(22) 语义）
- [本地模型](../guides/local-models.md) — NanoJev 决策模型画像与托管形态
- [工作流编辑器 UX](editor-ux-improvements.md) — EdFW 组件渲染细节
