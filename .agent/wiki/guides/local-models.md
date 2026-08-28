# 本地模型 · llama-server 部署与 agent 能力评估（local-lfm / local-lfm-vl）

> 2026-08 探针实测（run_python 直连 OpenAI 兼容端点）。结论先行：**local-lfm（2.6B）是合格的免费轻量 agent 工人——小模型有真 function calling + 可靠 JSON 提取**；local-lfm-vl 视觉通道半残（HTTP 500，待服务端 `--mmproj` 修复）。

## 端口与模型画像

| 服务 | 端口 | 模型 | 参数量 | 量化 | 特性 |
|------|------|------|--------|------|------|
| local-lfm | 8081 | lfm2.5 | 2.6B | QAD Q4_0 | `thinking=true`、60K 窗口、8192 max_tokens |
| local-lfm-vl | 8080 | lfm2.5-vl | 3B | Q4_K_M | vision、60K 窗口 |

速度实测：CPU 3~5 tok/s，单次调用 **18~38s**（vs 云端毫秒级——硬伤，但零 token 成本）。

## agent 能力实测（2026-08 探针）

| 维度 | 结果 | 证据 |
|------|------|------|
| 工具调用·单选+参数 | ✅ **远超预期** | read_file 行号区间 / list_dir 路径 / 参数 JSON 全对（3.5/4） |
| 工具调用·布尔参数 | ⚠️ | “正则搜索”填了 `regex: false`——字面语义理解偏差 |
| 工具调用·拒答 | ✅ | 无需工具时直接回答，不硬调 |
| JSON 结构化输出 | ✅ | `{"city":"杭州","days":3,"budget":2500}`——“两千五”→2500 都对 |
| 数学/数列推理 | ⚠️ | content 空，疑似答案落在 `reasoning_content`（thinking 模型字段分离，未确认） |
| 视觉（VL） | ❌ 待排查 | 图像请求 HTTP 500，大概率缺 `--mmproj` 或 vision 槽未启用 |
| 关键词提取（旧岗） | ✅ | 之前 session 实证：JSON schema 吃力，`\|` 分隔纯文本模式可用 |

## 用途建议（按适配度排序）

1. **攒批型 utility 任务**（最匹配）：wiki 维护批、夜间整理——18~38s 的速度硬伤在「不着急的批量」场景消失，且零 token 成本；ms-deepseek 端点不稳时它是备胎（**wiki-updater 的回退链加 local-lfm 即可**，见 [声明级回退链](../architecture/multi-agent.md)）
2. **子 Agent 简单任务**：分类 / 精排 / 格式转换——`agent_prompt` 派活的声明模型指 local-lfm，工具调用能力足以支撑 3~5 步小流程
3. **react_agent_demo 本地演示位**：工具调用四连已测通，能跑通 ReAct 原语演示（教学 / 调试用）
4. **local-lfm-vl 修复后**：vision 子 Agent 的免费备胎（qwen 视觉限流时回退）

**不适合**：react 主循环（速度）、复杂嵌套 schema、多工具并行选择（2.6B 上限明摆着，未测）。

## 待办（服务端配置，未实施）

```bash
# ① VL 500 修复：8080 启动命令加 mmproj（gguf 同目录一般有配套 mmproj gguf）
llama-server -m lfm2.5-vl-3b-q4_k_m.gguf --mmproj lfm2.5-vl-mmproj.gguf --port 8080 ...

# ② thinking 空 content：8081 加 --reasoning-format deepseek（把推理分离到 reasoning_content 字段）
#    或在 models.json 把该 provider 的 thinking: false——短调用不需要思考链，还能提速一倍
```

## 注意事项

- **8080 端口易主**：此前文档记录 local-qwen@8080（[wiki_auto_query 提词依赖](../features/wiki-auto-query.md)），现被 local-lfm-vl 占用——若 extract_keywords 仍指向 qwen 需改端口 / 换 provider，未核对前视为潜在断链
- **thinking 语义**：在 agt 里当 utility 用（recap_gen / 意图判断）建议关 thinking——短调用不需要思考链，还能提速；且 thinking 模型可能把答案放 `reasoning_content` 导致 content 空（②未修前）
- **服务未启动**：对应环节直接失败（同 local-qwen 依赖语义），攒批任务前先探活端口

## 相关页面

- [配置体系与模型调优](config-and-models.md)：models.json provider 档案 / thinking / vision 标志
- [wiki_auto_query 提词依赖](../features/wiki-auto-query.md)：8080 本地服务消费者（易主风险）
- [声明级回退链](../architecture/multi-agent.md)：回退链加 local-lfm 的落点
- [工具外置 · 判别标准](../architecture/tool-externalization-criteria.md)：攒批型任务可外置的边界背景
