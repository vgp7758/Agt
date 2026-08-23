// LLM 节点插件（type 3）：prompt/systemPrompt/结构化输出。
// params 协议：systemPrompt 与 prompt 都声明 canvas:true——画布节点下方渲染两个带徽章的大文本框
// （SYSTEM 在上 / PROMPT 在下，makeCanvasParamArea 自动按序偏移），props 面板同字段可编辑。
EdFW.register({
  type: "3",  label: "LLM",      icon: "🤖", category: "llm",
  section: "LLM 提示词",
  params: [
    { key: "systemPrompt", label: "SYSTEM", widget: "textarea", canvas: true,
      tip: "系统提示词（可 {{输入字段}} 引用上游输出）",
      get: lpGet("systemPrompt"), set: lpSet("systemPrompt") },
    { key: "prompt", label: "PROMPT", widget: "textarea", canvas: true,
      tip: "用户提示词，{{输入字段名}} 引用上游输出",
      get: lpGet("prompt"), set: lpSet("prompt") },
    { key: "model", widget: "select",
      options: () => Object.keys(AVAILABLE_MODELS || {}).sort(),
      hint: n => {
        const m = lpGet("model")(n);
        return m ? ("已选 " + m) : "（跟随 ctx.llm / utility）";
      },
      get: lpGet("model"), set: lpSet("model") },
    { key: "temperature", widget: "number",
      hint: n => { const t = lpGet("temperature")(n); return (t === "" || t == null) ? "（默认/全局）" : ""; },
      get: lpGet("temperature"), set: lpSet("temperature") },
    { key: "thinking", widget: "checkbox", label: "thinking",
      get: n => { const v = lpFind(n, "thinking"); return v == null ? "" : v; },
      set: (n, v) => lpSet("thinking")(n, v === true || v === "true" ? "true" : "false") },
    { key: "output_format", widget: "select", label: "输出格式",
      options: [["json", "json（按 schema 约束+解析字段）"], ["text", "text（纯文本，不解析）"]],
      hint: n => (lpGet("output_format")(n) === "text")
        ? "TEXT：不并入 schema 约束、不做结构化解析，content 原文从 output 端口输出"
        : "JSON：outputs 声明的结构并入 systemPrompt 约束，回包按字段强转解析",
      get: lpGet("output_format"), set: lpSet("output_format") },
    { key: "on_error", widget: "select", label: "出错时",
      options: [["fail", "中断（走 error 端口）"], ["empty", "输出空继续"]],
      get: lpGet("on_error"), set: lpSet("on_error") },
  ],
});
// llmParam 项读写辅助（文件级闭包——this 在 param.get/set 里指向 param 对象而非 register def，此前 this._lpGet 报 TypeError）
function lpFind(n, name){
  n.data.inputs = n.data.inputs || {};
  let lp = n.data.inputs.llmParam;
  if (!lp) lp = n.data.inputs.llmParam = [
    { name: "prompt", input: { type: "string", value: { type: "literal", content: "" } } },
    { name: "systemPrompt", input: { type: "string", value: { type: "literal", content: "" } } } ];
  let p = lp.find(x => x.name === name);
  if (!p && name === "systemPrompt") { p = { name, input: { type: "string", value: { type: "literal", content: "" } } }; lp.push(p); }
  return p;
}
function lpGet(name){
  return n => { const p = lpFind(n, name); return p?.input?.value?.content ?? ""; };
}
function lpSet(name){
  return (n, v) => { const p = lpFind(n, name); if (p) p.input.value.content = v; };
}
