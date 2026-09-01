# 配置体系与模型调优

> ~/.agt/models.json + ~/.agt/settings.json。改配置的命令：/config（CLI/WebUI 设置页）。

## models.json（provider 档案）

```jsonc
{
  "glm-utility": {
    "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    "api_token": "独立key",              // 或数组=["k1","k2"] 多号轮换
    "model": "glm-5.3",                  // 必须与 /v1/models 返回的 id 完全一致（含大小写/后缀！）
    "thinking": "low",                   // 三态（2026-08-31）：false=不发参数 / true=开关型思考模型发 enable_thinking（Qwen/ModelScope 类）/ "low"|"medium"|"high"|"max"=GLM 始终思考模型发 thinking={type:档位}——enable_thinking 对它 400 code 1210，详见下方「thinking 三态」节
    "vision": false,                     // 多模态能力（投影时 <img> 转 image_url）
    "max_effective_context_window": 60000, // 配了才启用分档投影；触发线=本值（顶窗才毕业折叠，commit 304bc16）
    "fold_target_ratio": 0.75,           // 保留水位占窗口比例（clamp 0.5~0.99；缺省 0.75，GLM 默认即可）——只决定触发后压到多低，不是触发阈值；DeepSeek（miss≈hit 60x）配低如 0.5：触发后压得狠、下次顶窗间隔长，小步升档（断尾部小）即可消化、少大折叠（断头部大）
    "detail_step": 0,                    // 组间步距衰减字数（clamp 0~200；缺省=全局 settings 15）——0=不衰减：渲染字节永不回缩、前缀缓存打满
    "requires_reasoning_in_history": false, // DeepSeek 思考模型=true：自动补历史 reasoning_content 占位防 400
    "token_rotate": true                 // 多token成功后预旋转；GLM等cache按token隔离的直连条目配false
  }
}
```

优先级：models.json > 项目根 models.py（gitignored，向后兼容）。

> `fold_target_ratio`/`detail_step` 是 per-provider 缓存经济学参数（用户提案 2026-08-30，commit 27fea56；触发语义修正 commit 304bc16）：GLM miss≈hit 4x 用默认 0.75 即可，DeepSeek ~60x 推荐三件套 `0.5 + 0 + token_rotate:false`（**低 ratio**=触发后压得狠、涨回窗口需 (1−ratio)×win、顶窗间隔长→折叠事件少——升档断尾部小、折叠断头部大，方向见 [方向澄清](../architecture/context-engine.md#方向澄清为什么-deepseek-配低-ratio-才对升档断尾部小折叠断头部大2026-08)）。机制见 [context-engine · per-provider 缓存经济学参数](../architecture/context-engine.md#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案)、触发线/保留线区分见 [触发线修复](../architecture/context-engine.md#触发线修复win-才是触发线winratio-是保留水位2026-08-30commit-304bc16)。

## 预设 provider 模板：models.preset.json 播种 + onboarding（2026-09-01，spec s_d4241d58，commit 7b8f156）

**用户提案（2026-09-01）**：新用户打开 WebUI 模型下拉框是空的——模型卡片有它的好处，但 provider 和用户都希望下拉框**开箱就有模型可选**。落地为**随包播种预设 provider**：`src/assets/models.preset.json`（4 provider × 9 模型），运行时只读合并进下拉视图，选中占位条目走 onboarding 弹窗引导申请 key 并落地用户配置。

**预设内容**（spec s_d4241d58）：modelscope（🎁 **free trial**——多数开源模型每日免费额度）/ zhipu 🔑 / deepseek-official 🔑 / siliconflow 🔑——各家带 `register_url`（申请页链接）+ `desc` + 模型参数（thinking/vision/desc 等，**模型级参数保持可配置**，落地时随条目写入）。

**api_tokens 占位语义**（预设条目专用，占位 token 永不真正使用）：

| 占位值 | 含义 | 前端行为 |
|---|---|---|
| `"free trial"` | 该 provider 有免费 token 额度 | 🎁 引导「领取免费额度」+ 输入 token |
| `"require an key"` | 须申请 key | 🔑 引导「打开申请页面」+ 输入 token |

**三条链路**（config.py + server.py + index.html，commit 7b8f156）：

| 层 | 内容 |
|---|---|
| config.py | `preset_models_view()` 合并视图（**用户已配置同名条目不在 preset 重复——用户优先**）+ `preset_entry()`（onboarding 拼 profile）——preset 只读，**不写用户 models.json** |
| server.py | `GET /api/models` 响应带 preset 字段；WS 连接/重连 system 消息带 preset（前端一次拿全）；**`POST /api/models/onboard`**：body `{name, api_keys}`（逗号分隔多 key，含中文逗号兼容）→ 预设参数 + 用户 token → 完整条目写 models.json（写生效份 + reload + 实例层热应用）→ 前端刷新下拉即可切换 |
| index.html | 模型下拉**分组渲染**：`✅ 已配置` optgroup + 预设 provider 分组（🎁/🔑 徽标）；选中 `preset::` 条目 → onboarding modal（provider 说明 + 「🌐 打开申请页面」新标签 + **多 key 输入**（数组保留多 key 配置能力））→ 落地后自动切换并刷新下拉 |

**中立性保留**：预设只是「开箱可选」——用户手动改 json 加自有 provider 的路径完全不变（`models.json` 条目永远优先于预设；想加预设外的 provider 就手动编辑 models.json）。

**生效方式**：后端新端点需 `/restart`；前端下拉 Ctrl+F5。

### onboarding 弹窗不可见真根因：modal-overlay 默认 display:none（2026-09-02，commit 5a30aa8）

**上节「看不到 UI 是浏览器缓存」的结论是误判——用户复查两次指出截图里没有弹窗，质疑完全正确，真根因在代码层**：

`.modal-overlay` 的 CSS 默认 `display:none`——settingsModal 用它时靠 JS 显式置 `flex` 才可见；`showPresetOnboard` 只做了 `document.body.appendChild(ov)`，没设 display → **DOM 在但视觉隐藏**（截图里没有弹窗正是此因）。

**误判链复盘**：night_tasks #2 的 playwright 验证只查了「DOM 存在」，没查 `getComputedStyle` 的实际 display 值——**DOM 存在 ≠ 视觉可见**。用户两次指出截图里没有弹窗，都被验证方法漏洞误读为「缓存」。

**修复一行**（src/static/index.html）：

```js
document.body.appendChild(ov);
ov.style.display = 'flex';   // .modal-overlay 的 CSS 默认 display:none——必须显式置 flex（settingsModal 同款），否则弹窗不可见
```

**复验（视觉级，playwright）**：display:flex / z-index:100 / 全屏遮罩（1440×769）/ token 输入框可见，截图 `onboard_modal_fixed_visible.png`。**Ctrl+F5 强刷即见**。

**教训**：弹窗类「看不到」排障顺序——① 先查 CSS 默认 display + JS 是否显式设置（`getComputedStyle`）→ ② 再怀疑浏览器缓存（Ctrl+F5）。「先强刷排除缓存」只解决浏览器层，代码层根因（display 未置）强刷无用；验证弹窗可见性必须查视觉状态（computed style），不能只查 DOM 存在性。

## Provider 参数硬约束规则表：base_url+model 预检查（2026-09-01，用户提案，commit 8c2fc6c）

**背景（用户提案 2026-09-01）**：各家 API 有已知硬约束（Kimi 温度必须 1、智谱 flash 不接受 enable_thinking、DeepSeek 思考模型必须补 reasoning 历史）——但这些约束在 models.preset.json 里没体现，且**手配模型（没走 onboarding）不受保护**。落地为**内置规则表 + 请求前自动修正**（用户无感知；profile 的 param_lock 是显式定制层，规则表是内置兜底层：手配模型未走 onboarding 也受保护，知识随版本分发）。

**规则表**（`src/llm_client.py` `_PROVIDER_PARAM_RULES`，`_match_provider_rules` 应用）：

```python
_PROVIDER_PARAM_RULES = [
    {"url_has": "moonshot",   "model_has": "imi",    "fix":  {"temperature": 1.0}},   # kimi 温度必须 1
    {"url_has": "modelscope", "model_has": "imi",    "fix":  {"temperature": 1.0}},   # 聚合端点同款
    {"url_has": "bigmodel",   "model_has": "flash",  "omit": ["enable_thinking"]},    # 智谱 flash 禁发
    {"url_has": "api.deepseek.com",                  "set":  {"requires_reasoning_in_history": True}},
]
```

**三种动作**：

| 动作 | 语义 |
|---|---|
| `fix` | 强制参数值——**覆盖一切来源**（请求 override 也改不回） |
| `omit` | 永不发送的参数（请求前剔除） |
| `set` | 自动置位 profile 级标志（如 requires_reasoning_in_history） |

匹配键 `url_has` / `model_has`：base_url / model id 的子串匹配（大小写敏感按原文）；`_build_kwargs` 组装时命中即应用。

**双层 + 优先级**：

```
内置规则 fix > profile param_lock > 请求 override > 全局默认
```

profile 显式定制层：`param_lock: ["temperature"]`（锁定参数无视 override）+ `param_omit: [...]`（永不发）——`_apply_profile` 读取、`_build_kwargs` 应用。预设 kimi 条目已带 `temperature: 1 + param_lock: ["temperature"]`（onboarding 落地双保险）。

**profile 显式优先于规则 set**：`requires_reasoning_in_history` 等键 profile 显式配置（true/false）优先；未显式配置（None）时保留规则表已 set 的置位——修掉了验证中发现的置位时序 bug（旧 `profile.get(key, False)` 默认值会覆盖规则 set）。

**验证（6 场景全过）**：kimi 强制 1（override 1.5 被修正）✓ / 智谱 flash omit ✓ / deepseek 自动置位 ✓ / profile 显式 False 不被规则覆盖 ✓ / param_lock 锁回 ✓ / 无命中原样 ✓。

以后发现新硬约束（如某端点不接受 top_p），往规则表加一行即可——用户无感知自动修正。`/restart` 后生效。

## settings.json（运行时）

| 键 | 说明 |
|----|------|
| fallback_chain | 回退链（逗号分隔 provider 名）；**运行时有效链 = _user_model 提前到链首** + base 链其余（/model 切换即重建） |
| fallback_policy | reset=每轮重试 _user_model（默认）；sticky=回退后不回 |
| utility_model | 统一辅助模型：recap/RAG检索/工作流LLM/reasoning补全默认 全走它（**必须独立 api_token**，见缓存坑） |
| detail_base / detail_step | 分档基准字数(1500)/步距衰减步长(15)——**detail_step 可被 models.json 条目级 `detail_step` 覆盖**（2026-08-30 起，clamp 0~200，0=不衰减；DeepSeek 类价差悬殊配 0），见 [per-provider 参数](../architecture/context-engine.md#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案) |
| 其余 | max_retries/temperature/enable_thinking/dump_projections（投影转储调试） |

> **回退链分层（2026-08，commit a667da4 起）**：settings 是**全局默认**；Agent 声明级 `fallback` 键（逗号串 / list / {chain,policy} 三形态）覆盖全局——[/agents 管理页表单化编辑](../features/agents-admin.md#回退链表单--钩子行布局修复2026-08commit-a667da4)（模型 chips 点选，留空=继承全局），`_main_` 主 Agent 同样支持；引擎侧解析见 [multi-agent · 声明级回退链](../architecture/multi-agent.md)。

## 配置文件解析 config_file：repo 级覆盖（2026-08-31，commit 10d717e）

四份配置文件（models.json / settings.json / main.yml / mcp.json）的解析统一走 `config.config_file(name)`（src/config.py，用户裁定 2026-08-31 · 多实例组网前置，commit 10d717e）：

```python
_AGT_DIR = Path.home() / ".agt"

def config_file(name: str) -> Path:
    _local = Path.cwd() / ".agent" / name
    return _local if _local.exists() else _AGT_DIR / name
```

| 规则 | 语义 |
|---|---|
| 优先级 | `<cwd>/.agent/<name>`（repo 级）→ `~/.agt/<name>`（全局兜底）；无本地文件时行为与旧版完全一致 |
| 覆盖粒度 | **文件级覆盖（非字段合并）**——本地存在即整份生效，全局同名文件被完全遮蔽 |
| 写侧跟随 | 读到哪份就写哪份：本地被读 → 保存写本地；全局被读 → 写全局（配置自治，不会「读 A 写 B」） |
| cwd 锚定 | import 时锚定（进程启动目录 = workspace，与 mcp_client 的 WORKSPACE 语义一致） |

**接入点**：`_AGT_MODELS` / `_AGT_SETTINGS` 两常量改走 `config_file`（加载 / 保存 / mtime 惰性重载全部自动跟随）；`seed_main_agent` 返回值（读侧本地优先，**播种仍写全局**、不覆盖本地独立主声明）；chat.py 两处 mcp 连接（`.mcp.json` 项目级原有职责不变——repo 覆盖是新增一层）；/agents 管理页 `_main_` 保存（写跟随读，note 动态显示实际路径，见 [agents-admin](../features/agents-admin.md)）。

**用途：多实例角色化的配置隔离**——每个角色实例（游戏组网的导演 / 多媒体等 repo）各持自己的四件套，认知（persona/assembly）与配置（模型/参数/MCP）双隔离，`~/.agt/` 全局只是无本地时的兜底。详见 [multi-instance · 配置 repo 级覆盖](../architecture/multi-instance.md)。

**注意**：`ensure_lsp` 的持久化仍固定写 `~/.agt/mcp.json`（lsp_manager `_GLOBAL_MCP`，设计如此「不写 cwd」）——repo 级 `.agent/mcp.json` 存在的实例**读不到后装的 LSP 条目**（本地整份遮蔽全局）。

验证（2026-08-31）：临时 cwd 带 `.agent/` 本地文件 → MODELS 只见本地条目（default 取本地）、settings 本地读（utility_model/max_level 本地值）、seed_main_agent 返回本地 main.yml；无本地对照 → 全局路径（现状完全不变）。8+1 项全过。

## 设置页配置来源切换：生效份 / 全局 / 本地（2026-08-31，commit ad0f385）

repo 级覆盖（上节）落地的是「**读侧自动**本地优先」；本节补**UI 显式选择**（用户裁定 2026-08-31 ·「设置页保存和读取时需要能选用全局还是本地」，commit ad0f385）——WebUI 设置弹窗（⚙️）顶部新增「配置来源」切换条：

```
配置来源：[生效份] [🌐 全局 ~/.agt] [📦 本地 .agent]   📦 本地覆盖生效中（徽章）
```

**三模式语义**：

| 模式 | 读取 | 保存 |
|---|---|---|
| **生效份**（默认，现状） | 自动本地优先 + 显示覆盖徽章（active_scope=local 时） | 写生效份 + 热应用（reload / apply_config） |
| **🌐 全局** | 显式载入 `~/.agt/` 那份的**原始文件视图**（模型卡片 + settings） | 写全局——**若本地覆盖生效中则存档备用、不热应用**（保存提示明确说明） |
| **📦 本地** | 显式载入 `.agent/` 覆盖份（不存在时提示「保存将新建」） | 写本地 + 若即生效份则热应用 |

**实现三层**：

| 层 | 内容 |
|---|---|
| src/config.py | **六个 scoped 函数**：`config_file_scoped(name, scope="auto")`（auto=`config_file` 现状 / local=强制 `<cwd>/.agent/` / global=强制 `~/.agt/`）+ `active_scope(name)`（本地文件存在='local' 覆盖生效中，否则 'global'）+ read/save × models/settings 四个 scoped 读写——**非生效份只落盘、不进运行时** |
| src/server.py | WS `get_config`/`set_config` 接受 scope；local/global 时返回 **`config_scoped` 事件**（该份原始 models+settings + active 标注，与 auto 的运行时视图分开）。REST `/api/models` GET/PUT 接受 scope（auto 响应附 `active` 字段，向后兼容） |
| src/static/index.html | scope-bar 切换条（`setCfgScope`/`cfgScope` 状态）+ 读取/保存全链带 scope + `fillSettingsForm(v)` 抽取共用——auto 的运行时视图与 scoped 的文件原始视图同一填表函数 |

**保存语义关键**：显式写**非生效份**只落盘不热应用——「改了 `~/.agt/` 但 `.agent/` 覆盖生效中」的存档备用场景由保存提示明确说明，避免「写了却看似没生效」的困惑；写生效份照旧走现状通道（保存后 reload + apply_config 热应用）。

验证（2026-08-31）：node --check + 9 项结构断言全过；**`/restart` + Ctrl+F5** 后生效。

用途与角色实例组网见 [multi-instance · 配置 repo 级覆盖](../architecture/multi-instance.md)——10d717e（读侧自动优先）+ ad0f385（UI 显式选择）拼成完整闭环：角色实例（游戏组网的导演/编剧/多媒体）可在各自 repo 的 WebUI 里直接管理自己的配置，也能查看/编辑全局兜底份。

## thinking 三态：bool 开关与档位字符串（GLM 始终思考模型，2026-08-31，commit 99f3bca）

**背景（用户提案 2026-08-31，commit 99f3bca）**：GLM 类「始终思考」模型（glm-5.x coding 系）对 `enable_thinking` 参数直接 **400 code 1210**「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」——provider 的 thinking 不该是 true/false 复选框，而是关/开/档位多选。落地为**三态**（models.json 条目 `"thinking"` 键）：

| `"thinking"` 值 | 请求发射（`extra_body`） | 适用 |
|---|---|---|
| `false`（缺省） | 不发任何参数 | 能力关（utility 短调用；对 GLM 始终思考模型也安全——不发就不 400，但档位不可控） |
| `true`（旧语义） | `{"enable_thinking": true/false}`（false 也发——该类模型支持显式关） | Qwen/ModelScope 类开关型思考模型 |
| `"low"` / `"medium"` / `"high"` / `"max"`（档位字符串） | `{"thinking": {"type": 档位}}`，**enable_thinking 永不发** | GLM 类「始终思考」模型 |

**实现三层**：

| 层 | 内容 |
|---|---|
| src/llm_client.py | profile 解析出 `thinking_type`（档位字符串；非四档之一的 str 回退 None 走 bool 语义）。`_build_kwargs` 发射优先级：**per-node 档位（overrides `thinking_tier`）> profile 档位 > enable_thinking**——档位模式下 enable_thinking override（全局 `/config`、per-node bool）全部忽略；非档位模型也可被节点单独指定档位 |
| src/static/index.html | 模型卡片 thinking 复选框 → **六选下拉**（思考：关 / 开 / low / medium / high / max）+ badge 显示档位名（🧠 low；true 仍显 thinking）；旧数据 bool 完全兼容（true→开 / false→关） |
| src/assets/nodes_builtin/llm.py | llm(3) 节点 per-node `thinking` 支持档位字符串 → overrides `thinking_tier`（此前统一 bool 转换、档位会被吃掉），见 [workflow-hooks · llm 节点 thinking 档位](../architecture/workflow-hooks.md#llm3-节点-per-node-thinking-档位2026-08-31commit-99f3bca) |

**为什么要档位**：思考模型惯例配 `thinking: true` → 对 GLM 始终思考模型每次调用都发 `enable_thinking` → **必 400**（utility/回退链第一跳总是先炸一次再退避，8 月底 recap_gen 排障里反复出现的「utility 400『始终思考』」即此）。配档位后该条目改发 `thinking={"type":"low"}`——全局 `/config enable_thinking`、工作流 per-node thinking 打到它身上都不再触发 400，第一跳直接成功。

**操作**：`/restart` + Ctrl+F5，设置 → 模型管理把 GLM 条目（utility / glm-official）的 thinking 从「开」改成档位（如 `low`）。400 症状条目见 [ops · 常见错误对照](ops.md#常见错误对照)。

## 模型能力标志速查

| 场景 | 配置 |
|------|------|
| 长会话上下文压缩 | `max_effective_context_window`（不配=全量投影，长会话必爆） |
| DeepSeek 思考模型混用历史 | `requires_reasoning_in_history: true`（规则表已自动置位；profile 显式配置优先） |
| GLM 直连多 token | `token_rotate: false` + utility 分开条目 |
| GLM 始终思考模型（glm-5.x coding，2026-08-31） | `thinking` 配**档位字符串**（如 `"low"`）——发 `thinking={type:档位}`、永不发 enable_thinking（该参数 400 code 1210），见 [thinking 三态](#thinking-三态bool-开关与档位字符串glm-始终思考模型2026-08-31commit-99f3bca) |
| Kimi K3（moonshot / modelscope 聚合，2026-09-01） | `temperature` 锁 **1**（API 硬约束）——规则表 fix 强制 + preset 条目 `param_lock` 双保险，见 [硬约束规则表](#provider-参数硬约束规则表base_urlmodel-预检查2026-09-01用户提案commit-8c2fc6c) |
| DeepSeek v4 缓存敏感（2026-08-29 实证） | **miss 单价 ≈ hit 的 30-50 倍**（GLM 仅 ~4 倍），长会话慎用大 prompt；**变化的 system 消息 / tools 列表变化 → 全序列缓存断**——动态注入必须 user role（框架已修，见 [缓存行为实证](../architecture/context-engine.md#deepseek-缓存行为实证v3-位置敏感--v4-system-规范化2026-08-两代后端)）。多 token per-token 隔离嫌疑已否证（单 token 同样断），多 token 无需特殊配置。**经济学对策三件套（2026-08-30，commit 27fea56）：`fold_target_ratio: 0.5`（低——触发后压得狠、顶窗间隔长，升档即可消化、少大折叠）+ `detail_step: 0`（组间不衰减）+ `token_rotate: false`（sticky）**——方向见 [方向澄清](../architecture/context-engine.md#方向澄清为什么-deepseek-配低-ratio-才对升档断尾部小折叠断头部大2026-08)、机制见 [per-provider 缓存经济学参数](../architecture/context-engine.md#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案)、触发线语义见 [触发线修复](../architecture/context-engine.md#触发线修复win-才是触发线winratio-是保留水位2026-08-30commit-304bc16) |
| ModelScope 多号额度 | 默认预旋转（true），无需配置 |
| 视觉模型 | `vision: true`（read_file 读图片自动压缩到 2048 边长） |

## 本地模型（llama-server，local-lfm 系列）

本地两个 llama-server 模型（CPU 部署）：`local-lfm`@8081（lfm2.5-2.6B，`--reasoning-format deepseek`）与 `local-lfm-vl`@8080（lfm2.5-vl-3B，`--mmproj` 已挂载）。2026-08 探针评估结论：**2.6B 有真 function calling + 可靠 JSON 提取**，适合攒批型 utility / 子 Agent 简单任务 / react demo；速度 18~38s/次是硬伤。**此前两个服务端坑均已修（2026-08 改启动脚本，重启生效）**：VL 视觉通道 500 → 启动命令加 `--mmproj`；thinking content 空 → 加 `--reasoning-format deepseek` 分离思考链（见 [启动脚本与坑的修复](local-models.md#启动脚本bat-三件套2026-08-起)）。残余注意：thinking 小模型短调用要留思考余量（`max_tokens` 抠太紧会被思考耗尽致 content 空）——**utility 场景已配 `thinking:false` 干脆不请求思考链（2026-08-30，commit e8ef64a，recap_gen 首个落地，~38s→~15s）**。完整能力矩阵与用途建议见 [本地模型评估](local-models.md)。

## 踩坑记录

1. **model id 必须逐字符核对**：`deepseek-ai/DeepSeek-V4-Pro-0813` 不存在（正确 id 无 -0813 后缀）→ BadRequestError 400 "has no provider supported"。用 /v1/models 接口核对
2. **429 insufficient balance**：ModelScope 按号限额，token 用尽报此错；限流轮换会自动切下一个（多 token 分摊）。**bigmodel 直连余额耗尽同样报此错**（2026-08-30 实测：utility 的 glm 通道欠费 → recap 等场景批量 429，319 条失败；充值或 `/config utility_model` 换通道）
3. **proxy 聚合端**：stats 里 model=proxy 的记录看不到真实路由——resp_model 字段（0.17.2+）按 `provider/回包模型` 分端点展示；旧数据用 tools/clean_llm_calls.py 清洗
4. **500/502/503**：InternalServerError 已纳入回退捕获（旧版漏掉会直接崩）——确认 agt ≥ 0.16.2
5. **改窗口配置后 live 进程不生效（0.22.2 修复，commit f57de5d）**：`max_effective_context_window` 是 llm 创建时固化进 session 的**副本**——`/reload models` 此前只刷 profile 不同步副本，毕业/折叠仍按旧窗口算（实测：改 700K 仍按 256K 档的 192K 目标线每轮毕业）。现四入口全同步（`/model` 切换 / `/reload models` / WebUI 保存模型配置 / `/config`），reload 输出 `· session 窗口已同步：…（折叠目标线 … tok）` 即生效；**直接改 models.json 文件后跑一次 `/reload models`（0.22.2+）或 `/restart`**。详见 [context-engine · 窗口值生命周期](../architecture/context-engine.md#窗口值生命周期llm-固化副本--改窗口四入口同步2026-08-29commit-f57de5d)
6. **工作流 LLM 节点的 model 是独立 `<model>` 标签，不是 `<param name="model">`（2026-08-30 误诊教训）**：type3 节点序列化形态见 workflow_xml.py 写侧——grep param 形态搜不到 ≠ model 未设置（曾据此错判 recap_gen「未设置→utility 兜底」，真实是 `<model>proxy</model>` → glm 429）。另编辑器改模型**不点保存不落盘**，「日志还是旧模型」先读磁盘 XML 再怀疑刷新。详见 [workflow-hooks · 双格式与热加载](../architecture/workflow-hooks.md#双格式与热加载)
7. **轮边界「每轮毕业」第二例：触发判据错用保留线（commit 304bc16 修复，用户诊断）**：`_plan_fold` 入口判据曾是 `est > win×fold_ratio`（把保留水位当触发线）——投影在 target~win 健康区间（如 400K 窗口下 280K~400K）**每轮升档毕业**。v0.22.3 语义定稿「ratio=保留水位非触发阈值」时文档写对了、代码未对齐，错位一个版本周期。修复后触发线=win 本身、触发后压到 win×ratio；未触发区间零动作纯追加（前缀缓存最优）。**排障口诀**：先看旁车 `proj_stats.json` 的 `win`/`fold_target`——窗口值错=踩坑 5（配置同步问题）；窗口值对还每轮毕业=本条（判据问题，升级 ≥ 修复版本）。详见 [context-engine · 触发线修复](../architecture/context-engine.md#触发线修复win-才是触发线winratio-是保留水位2026-08-30commit-304bc16)
8. **MODELS 启动时快照：新增 provider 条目运行时不知道 → LLM 节点静默回退（recap_gen 三轮排障收官；0d852a0 起 warning、85a41fd 根因修复）**：长寿进程启动于条目加入 models.json **之前** → 运行时 `get_profile(新键)` KeyError → 工作流 LLM 节点 `_get_llm` 静默回退 `ctx.llm`（utility）——表象「工作流选了 X、llm_calls 却全是 utility + 回退链」。**判别口诀：llm_calls.jsonl 没有一条 X 的记录 = 请求从未发出 = 换模型发生在请求前（get_llm 层）；有 X 记录（400/429）才是 provider 侧问题**。修复两步：0d852a0 回退打 warning（一眼定位）；**85a41fd 根因修复——`get_profile` 入口惰性 mtime 重载**（`_maybe_reload_models`：stat 一次开销可忽略，mtime 变了自动 `reload_models()`；`_MODELS_RELOADING` 重入保护防 reload 内部 get_profile 递归）——手改 models.json 加条目后**运行中进程下次取 profile 自动可见，无需 /reload models**。与坑 5 同源不同症状——坑 5 是已有条目的**窗口值**不同步，本条是**整条目缺失**（子进程测试全通、运行时不通的谜底）。详见 [workflow-hooks · `_get_llm` 静默 fallback 与 MODELS 惰性重载](../architecture/workflow-hooks.md#_get_llm-静默-fallback-加日志与-models-惰性重载根因修复2026-08)
9. **轮边界「每轮毕业」第三例：触发判定估算分子 fc=0 假想（commit 95f9a00 修复，用户报告「实测 381K<win 仍每轮毕业」）**：304bc16 已把判据改对（顶窗才触发），但入口估算仍硬编码 `_render_tiered_history(0)`（**假装零折叠渲染全部历史**）——对已折叠 session（fc=282）假想恒超线（856K vs 实际投影 320K），每轮 start_turn 进压力路径升档顺移，即使真实投影 336K 远低于 win=500K。修复：估算改用**实际生效折叠计划 `_planned_fold`** 渲染（fc=0 时两口径等价），验证 est 539,683 → 336,130 < 500K 零动作。**排障口诀补全（三案例）**：旁车 `win`/`fold_target` 值错=坑 5（窗口副本同步）；值对+判据错=坑 7（保留线当触发线）；**值对+判据对仍每轮毕业=本条（估算分子假想，升级 ≥ 修复版本）**。详见 [context-engine · 触发判定估算口径](../architecture/context-engine.md#触发判定估算口径fc0-假想--实际折叠计划2026-08-31commit-95f9a00用户报告)

