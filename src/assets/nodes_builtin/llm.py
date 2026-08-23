"""LLM 节点插件（type 3）：渲染 prompt/systemPrompt，调用 ctx.llm（可指定 model）。

声明的 outputs 结构自动并入 systemPrompt 约束输出格式；多字段/结构化声明
（非单个 output:string）时按字段名强转解析展开，失败降级 {output: 原文}。
"""
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
    return {"type": "3", "label": "LLM", "handler": _handle_llm}
