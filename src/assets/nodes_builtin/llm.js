// LLM 节点插件（type 3）：prompt/systemPrompt/结构化输出。
// params 协议：每个 param 声明画布/props 渲染样式（widget）与读写（get/set）；
// canvas:true 的 param（prompt）在画布节点下方渲染大文本框直编。
// 注意：param 的 get/set 里不能引用 this._lpXxx（this=param 对象，无该方法）——
// 用文件级闭包函数 lpGet/lpSet。
function lpGet(n, k) {
  const lp = n.data.inputs.llmParam || [];
  return lp.find(x => x.name === k)?.input?.value?.content ?? "";
}
function lpSet(n, k, v) {
  let lp = n.data.inputs.llmParam || (n.data.inputs.llmParam = []);
  let p = lp.find(x => x.name === k);
  if (!p) { p = { name: k, input: { type: 'string', value: { type: 'literal', content: '' } } }; lp.push(p); }
  p.input.value.content = v;
}
EdFW.register({
  type: "3",  label: "LLM",      icon: "🤖", category: "llm",
  section: "LLM 提示词",
  params: [
    { key: "model", label: "模型", widget: "select",
      options: () => [["", "（跟随主Agent）"], ...Object.entries(AVAILABLE_MODELS).map(([k, v]) => [k, v || k])],
      tip: "空=跟随 ctx.llm（utility/主模型）",
      get: n => lpGet(n, 'model'), set: (n, v) => lpSet(n, 'model', v) },
    { key: "output_format", label: "输出格式", widget: "select",
      options: [["json", "JSON（结构化解析）"], ["text", "TEXT（原文透传）"]],
      hint: n => (lpGet(n, 'output_format') || "json") === "text"
        ? "TEXT：不并入 schema 约束、不做结构化解析，output=content 原文"
        : "JSON：outputs 结构并入 systemPrompt 约束并按字段解析展开",
      get: n => lpGet(n, 'output_format') || "json", set: (n, v) => lpSet(n, 'output_format', v) },
    { key: "thinking", label: "思考", widget: "select",
      options: [["", "（跟随默认）"], ["true", "开"], ["false", "关"]],
      get: n => lpGet(n, 'thinking'), set: (n, v) => lpSet(n, 'thinking', v) },
    { key: "temperature", label: "温度", widget: "number", tip: "0~2，空=跟随默认",
      get: n => lpGet(n, 'temperature'), set: (n, v) => lpSet(n, 'temperature', v) },
    { key: "systemPrompt", label: "systemPrompt", widget: "textarea", canvas: false,
      tip: "角色/格式约束；{{输入字段}} 占位符可用",
      get: n => lpGet(n, 'systemPrompt'), set: (n, v) => lpSet(n, 'systemPrompt', v) },
    { key: "prompt", label: "prompt", widget: "textarea", canvas: true,
      tip: "用户提示词，{{输入字段名}} 引用上游输出",
      get: n => lpGet(n, 'prompt'), set: (n, v) => lpSet(n, 'prompt', v) },
  ],
});
