# 本地模型 · llama-server 部署与 agent 能力评估（local-lfm / local-lfm-vl）

> 2026-08 探针实测（run_python 直连 OpenAI 兼容端点）。结论先行：**两个模型均已完整可用**——local-lfm（2.6B）是合格的免费轻量 agent 工人（真 function calling + 可靠 JSON 提取 + 思考分离）；local-lfm-vl（3B）视觉通道已修通（`--mmproj` 挂载），可当 vision 子 Agent 免费备胎，但**小字 OCR 是弱项**。

## 端口与模型画像

| 服务 | 端口 | 模型 | 参数量 | 量化 | 特性 |
|------|------|------|--------|------|------|
| local-lfm | 8081 | lfm2.5 | 2.6B | QAD Q4_0 | `--reasoning-format deepseek`、60K 窗口、8192 max_tokens |
| local-lfm-vl | 8080 | lfm2.5-vl | 3B | Q4_K_M | vision（mmproj 已挂载）、60K 窗口 |

速度实测：CPU 3~5 tok/s，单次调用 **18~38s**（vs 云端毫秒级——硬伤，但零 token 成本）。

## agent 能力实测（2026-08 探针）

| 维度 | 结果 | 证据 |
|------|------|------|
| 工具调用·单选+参数 | ✅ **远超预期** | read_file 行号区间 / list_dir 路径 / 参数 JSON 全对（3.5/4） |
| 工具调用·布尔参数 | ⚠️ | “正则搜索”填了 `regex: false`——字面语义理解偏差 |
| 工具调用·拒答 | ✅ | 无需工具时直接回答，不硬调 |
| JSON 结构化输出 | ✅ | `{"city":"杭州","days":3,"budget":2500}`——“两千五”→2500 都对 |
| 数学/数列推理 | ✅ 已翻案 | content 空不是模型不会——思考链早算出 42，是 `max_tokens=250` 被思考耗尽；放宽到 800：思考 224 tok + 输出 "42"（[thinking 小模型要留思考余量](#注意事项)） |
| 视觉（VL） | ✅ 已修通 | `--mmproj` 挂载后图像理解复活：“图中有两个形状：一个蓝色的正方形，一个红色的圆形”（22s，颜色形状全对）；**小字 OCR 不行**（"agt-agent 0.22" 没认出来） |
| 关键词提取（旧岗） | ✅ | 之前 session 实证：JSON schema 吃力，`\|` 分隔纯文本模式可用 |

## 启动脚本（bat 三件套，2026-08 起）

脚本位于 `D:\Programs\env\`（workspace 副本 `tools/start-lfm.bat`、`tools/start-lfm-vl.bat` 已同步，命名与 local-qwen.bat 一致）：

| 脚本 | 服务 | 关键参数 |
|------|------|----------|
| start-lfm.bat | local-lfm@8081 | `--reasoning-format deepseek`（思考分离） |
| start-lfm-vl.bat | local-lfm-vl@8080 | `--mmproj mmproj-lfm2.5-vl-3b-bf16.gguf`（视觉挂载） |

2026-08 深夜热调试把此前两个服务端坑全部修掉（commit 8e43496；重启服务即生效）：

- **① VL 500 → 已修**：8080 启动命令加 `--mmproj`——mmproj 文件本来就在模型目录里躺着，缺参是 500 根因
- **② thinking 空 content → 已修**：8081 加 `--reasoning-format deepseek`，推理链分离到 `reasoning_content`，`content` 干净输出，finish=stop

（原「待办（服务端配置，未实施）」两条至此全部落地。）

## 用途建议（按适配度排序）

1. **攒批型 utility 任务**（最匹配）：wiki 维护批、夜间整理——18~38s 的速度硬伤在「不着急的批量」场景消失，且零 token 成本；ms-deepseek 端点不稳时它是备胎（**wiki-updater 的回退链加 local-lfm 即可**，见 [声明级回退链](../architecture/multi-agent.md)）
2. **子 Agent 简单任务**：分类 / 精排 / 格式转换——`agent_prompt` 派活的声明模型指 local-lfm，工具调用能力足以支撑 3~5 步小流程
3. **react_agent_demo 本地演示位**：工具调用四连已测通，能跑通 ReAct 原语演示（教学 / 调试用）
4. **local-lfm-vl（已就位）**：vision 子 Agent 的免费备胎（qwen 视觉限流时回退）——形状 / 颜色 / 布局理解 OK；**精细文字识别别指望 3B**

**不适合**：react 主循环（速度）、复杂嵌套 schema、多工具并行选择（2.6B 上限明摆着，未测）。

## 注意事项

- **thinking 小模型要留思考余量**（本轮最大教训）：`--reasoning-format deepseek` 后答案落在 `content`，但思考链先吃掉 token——`max_tokens` 抠太紧（如 250）会被思考耗尽导致 content 空。models.json 里 8192 没问题，是测试参数抠了；通用规则：短调用给 thinking 模型留足「思考链 + 输出」的余量
- **utility 短调用可关 thinking**：recap_gen / 意图判断不需要思考链，`thinking: false` 还能提速一倍
- **8080 端口易主**：此前文档记录 local-qwen@8080（[wiki_auto_query 提词依赖](../features/wiki-auto-query.md)），现被 local-lfm-vl 占用——若 extract_keywords 仍指向 qwen 需改端口 / 换 provider，未核对前视为潜在断链
- **服务未启动**：对应环节直接失败（同 local-qwen 依赖语义），攒批任务前先探活端口；开机起服务直接双击 `D:\Programs\env\` 下的 bat（可同时开两个窗口起全套）

## 相关页面

- [配置体系与模型调优](config-and-models.md)：models.json provider 档案 / thinking / vision 标志
- [wiki_auto_query 提词依赖](../features/wiki-auto-query.md)：8080 本地服务消费者（易主风险）
- [声明级回退链](../architecture/multi-agent.md)：回退链加 local-lfm 的落点
- [工具外置 · 判别标准](../architecture/tool-externalization-criteria.md)：攒批型任务可外置的边界背景
