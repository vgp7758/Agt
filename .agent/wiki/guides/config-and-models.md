# 配置体系与模型调优

> ~/.agt/models.json + ~/.agt/settings.json。改配置的命令：/config（CLI/WebUI 设置页）。

## models.json（provider 档案）

```jsonc
{
  "glm-utility": {
    "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
    "api_token": "独立key",              // 或数组=["k1","k2"] 多号轮换
    "model": "glm-5.3",                  // 必须与 /v1/models 返回的 id 完全一致（含大小写/后缀！）
    "thinking": false,                   // 思考模型 true；utility 短调用建议 false
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

## settings.json（运行时）

| 键 | 说明 |
|----|------|
| fallback_chain | 回退链（逗号分隔 provider 名）；**运行时有效链 = _user_model 提前到链首** + base 链其余（/model 切换即重建） |
| fallback_policy | reset=每轮重试 _user_model（默认）；sticky=回退后不回 |
| utility_model | 统一辅助模型：recap/RAG检索/工作流LLM/reasoning补全默认 全走它（**必须独立 api_token**，见缓存坑） |
| detail_base / detail_step | 分档基准字数(1500)/步距衰减步长(15)——**detail_step 可被 models.json 条目级 `detail_step` 覆盖**（2026-08-30 起，clamp 0~200，0=不衰减；DeepSeek 类价差悬殊配 0），见 [per-provider 参数](../architecture/context-engine.md#per-provider-缓存经济学参数fold_target_ratio--detail_step2026-08-30commit-27fea56用户提案) |
| 其余 | max_retries/temperature/enable_thinking/dump_projections（投影转储调试） |

> **回退链分层（2026-08，commit a667da4 起）**：settings 是**全局默认**；Agent 声明级 `fallback` 键（逗号串 / list / {chain,policy} 三形态）覆盖全局——[/agents 管理页表单化编辑](../features/agents-admin.md#回退链表单--钩子行布局修复2026-08commit-a667da4)（模型 chips 点选，留空=继承全局），`_main_` 主 Agent 同样支持；引擎侧解析见 [multi-agent · 声明级回退链](../architecture/multi-agent.md)。

## 模型能力标志速查

| 场景 | 配置 |
|------|------|
| 长会话上下文压缩 | `max_effective_context_window`（不配=全量投影，长会话必爆） |
| DeepSeek 思考模型混用历史 | `requires_reasoning_in_history: true` |
| GLM 直连多 token | `token_rotate: false` + utility 分开条目 |
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

