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
    "max_effective_context_window": 60000, // 配了才启用分档投影
    "requires_reasoning_in_history": false, // DeepSeek 思考模型=true：自动补历史 reasoning_content 占位防 400
    "token_rotate": true                 // 多token成功后预旋转；GLM等cache按token隔离的直连条目配false
  }
}
```

优先级：models.json > 项目根 models.py（gitignored，向后兼容）。

## settings.json（运行时）

| 键 | 说明 |
|----|------|
| fallback_chain | 回退链（逗号分隔 provider 名）；**运行时有效链 = _user_model 提前到链首** + base 链其余（/model 切换即重建） |
| fallback_policy | reset=每轮重试 _user_model（默认）；sticky=回退后不回 |
| utility_model | 统一辅助模型：recap/RAG检索/工作流LLM/reasoning补全默认 全走它（**必须独立 api_token**，见缓存坑） |
| detail_base / detail_step | 分档基准字数(1500)/步距衰减步长(15) |
| 其余 | max_retries/temperature/enable_thinking/dump_projections（投影转储调试） |

## 模型能力标志速查

| 场景 | 配置 |
|------|------|
| 长会话上下文压缩 | `max_effective_context_window`（不配=全量投影，长会话必爆） |
| DeepSeek 思考模型混用历史 | `requires_reasoning_in_history: true` |
| GLM 直连多 token | `token_rotate: false` + utility 分开条目 |
| ModelScope 多号额度 | 默认预旋转（true），无需配置 |
| 视觉模型 | `vision: true`（read_file 读图片自动压缩到 2048 边长） |

## 踩坑记录

1. **model id 必须逐字符核对**：`deepseek-ai/DeepSeek-V4-Pro-0813` 不存在（正确 id 无 -0813 后缀）→ BadRequestError 400 "has no provider supported"。用 /v1/models 接口核对
2. **429 insufficient balance**：ModelScope 按号限额，token 用尽报此错；限流轮换会自动切下一个（多 token 分摊）
3. **proxy 聚合端**：stats 里 model=proxy 的记录看不到真实路由——resp_model 字段（0.17.2+）按 `provider/回包模型` 分端点展示；旧数据用 tools/clean_llm_calls.py 清洗
4. **500/502/503**：InternalServerError 已纳入回退捕获（旧版漏掉会直接崩）——确认 agt ≥ 0.16.2
