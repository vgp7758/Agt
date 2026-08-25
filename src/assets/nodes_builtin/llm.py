"""LLM 节点插件（type 3）：渲染 prompt/systemPrompt，调用 ctx.llm（可指定 model）。

声明的 outputs 结构自动并入 systemPrompt 约束输出格式；多字段/结构化声明
（非单个 output:string）时按字段名强转解析展开，失败降级 {output: 原文}。
"""

# 节点参数声明（前端 .js 同款 params 的后端镜像：文档/校验/工具化用）
PARAMS = [
    {"key": "prompt",       "type": "string", "required": True,
     "desc": "用户提示词模板，支持 {{输入字段名}} 占位符引用上游输出"},
    {"key": "systemPrompt", "type": "string", "required": False,
     "desc": "系统提示词（角色/格式约束）；outputs 声明会自动并入 JSON Schema 约束"},
    {"key": "model",        "type": "string", "required": False, "default": "",
     "desc": "models.json provider 名；空=跟随 ctx.llm（utility/主模型）"},
    {"key": "thinking",     "type": "string", "required": False, "enum": ["", "true", "false"],
     "desc": "思考开关；空=跟随默认"},
    {"key": "timeout",      "type": "number", "required": False,
     "desc": "请求超时秒数；空=全局默认"},
    {"key": "onError",      "type": "string", "required": False,
     "desc": "失败时输出的替代文本；空=中断工作流"},
    {"key": "output_format","type": "string", "required": False, "enum": ["json", "text"], "default": "json",
     "desc": "json=声明 outputs 结构化解析；text=不约束不解析，content 原文走 output 端口"},
]

import json

from workflow_node_api import (resolve_value, resolve_input_params, render_template, get_llm,
                               outputs_to_json_schema, needs_structured_parse, parse_structured_output)


def _handle_llm(node: dict, ctx) -> dict:
    inputs = node.get("data", {}).get("inputs", {})
    params = resolve_input_params(inputs.get("inputParameters", []), ctx)

    cfg = {}
    for p in inputs.get("llmParam", []):
        cfg[p.get("name")] = resolve_value(p.get("input"), ctx)

    prompt = render_template(str(cfg.get("prompt", "")), params)
    system = render_template(str(cfg.get("systemPrompt", "")), params).strip()

    # 输出格式：json（默认）= 声明的 outputs 结构并入 systemPrompt 约束 + 按 JSON Schema 解析展开字段；
    # text = 纯文本——不并入 schema 约束、不做结构化解析，content 原文直接从 output 端口输出
    output_format = str(cfg.get("output_format") or "json").strip().lower() or "json"

    # 把节点声明的输出结构转成 JSON Schema，并入系统提示词
    outputs = node.get("data", {}).get("outputs", []) or []
    if outputs and output_format == "json":
        schema = outputs_to_json_schema(outputs)
        schema_hint = ("\n\n【输出要求】请严格按照以下 JSON Schema 输出（纯 JSON，不要 markdown 代码块，不要多余解释）：\n"
                       + json.dumps(schema, ensure_ascii=False, indent=2))
        system = (system + schema_hint) if system else schema_hint.strip()

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt or "（空提示）"})

    overrides = {}
    if cfg.get("temperature") is not None:
        try:
            overrides["temperature"] = float(cfg["temperature"])
        except (TypeError, ValueError):
            pass
    if cfg.get("thinking") is not None:   # per-node 开关覆盖实例默认（推理模型）
        overrides["enable_thinking"] = str(cfg.get("thinking")).strip().lower() in ("true", "1", "yes", "on")
    if cfg.get("timeout") is not None:
        try:
            overrides["timeout"] = float(cfg["timeout"])
        except (TypeError, ValueError):
            pass
    llm = get_llm(ctx, str(cfg.get("model", "") or ""))
    on_error = cfg.get("onError")
    try:
        resp = llm.chat(msgs, **overrides)
    except Exception:
        if on_error:   # 配了 onError：不中断工作流，输出反馈文本
            return {"outputs": {"output": str(on_error)}, "port": None}
        raise
    content = getattr(resp, "content", "") or ""
    reasoning = getattr(resp, "reasoning", "") or ""
    outs = {"output": content}
    if reasoning:
        outs["reasoning"] = reasoning   # 推理模型的思考过程默认带上，供下游引用/调试查看
    if output_format != "text" and outputs and needs_structured_parse(outputs):
        parsed = parse_structured_output(content, outputs)
        if parsed is not None:
            # 保留原文便于调试；但勿覆盖用户声明的 output 字段——声明为 object 时它是结构化结果，
            # 下游 .output.found 要能取到（被原文覆盖成字符串会让 selector 子字段引用失效）
            if "output" in parsed:
                parsed["_raw"] = content
            else:
                parsed["output"] = content
            if reasoning:
                parsed["reasoning"] = reasoning
            outs = parsed
    return {"outputs": outs, "port": None}


def agt_node():
    return {"type": "3", "label": "LLM", "handler": _handle_llm, "catalog": _CATALOG}

# ===== 节点目录条目（list_workflow_nodes / query_workflow_node 动态聚合自插件声明）=====
_CATALOG = {"name": "LLM", "desc": "调用大语言模型，支持模板渲染 {{变量}}、systemPrompt、temperature/maxTokens 等参数配置，可声明结构化输出 schema", "xml": "<!-- LLM 节点：调用大模型 -->\n<node id=\"130001\" type=\"llm\">\n  <!-- 模板入参：声明后在 prompt/systemPrompt 中用 {{变量名}} 引用 -->\n  <in name=\"query\" ref=\"100001.query\"/>\n  <in name=\"context\" ref=\"120001.result\"/>\n\n  <!-- LLM 参数（param）：在 Coze 中等同于 llmParam -->\n  <param name=\"prompt\"><![CDATA[根据以下上下文回答问题：{{query}}\n\n上下文：\n{{context}}]]></param>\n  <param name=\"systemPrompt\"><![CDATA[你是专业的问答助手。回答简洁准确，不超过 200 字。]]></param>\n  <param name=\"temperature\" type=\"float\">0.7</param>\n  <param name=\"maxTokens\" type=\"integer\">1024</param>\n  <param name=\"modelName\"><![CDATA[deepseek-chat]]></param>\n\n  <!-- 结构化输出（可选）：声明 output 字段及其 schema，LLM 将按 JSON Schema 输出 -->\n  <out name=\"output\" type=\"string\"/>\n  <out name=\"answer\" type=\"string\"/>\n  <out name=\"confidence\" type=\"integer\"/>\n</node>\n<!--\n  llmParam 可用参数：prompt, systemPrompt, temperature, maxTokens, modelName, topP\n  输入：in 声明的模板变量（在 prompt 中用 {{变量名}} 引用）\n  输出：output（LLM 原始输出）；若声明了多个 out 字段，LLM 将输出符合 schema 的 JSON\n-->"}
