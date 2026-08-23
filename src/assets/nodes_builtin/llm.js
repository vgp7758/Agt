// LLM 节点插件（type 3）：prompt/systemPrompt/结构化输出。
// params 协议：每个 param 声明画布/props 渲染样式（widget）与读写（get/set）；
// canvas:true 的 param（prompt）在画布节点下方渲染大文本框直编。
EdFW.register({
  type: "3",  label: "LLM",      icon: "🤖", category: "llm",
  section: "LLM 提示词",
  _lpGet(n, k) {           // llmParam 读取助手
    const lp = n.data.inputs?.llmParam || [];
    return lp.find(x => x.name === k)?.input?.value?.content ?? "";
  },
  _lpSet(n, k, v) {        // 写入（不存在则建槽位）
    let lp = n.data.inputs.llmParam || (n.data.inputs.llmParam = []);
    let p = lp.find(x => x.name === k);
    if (!p) { p = { name: k, input: { type: 'string', value: { type: 'literal', content: '' } } }; lp.push(p); }
    p.input.value.content = v;
  },
  params: [
    { key: "model", label: "模型", widget: "select",
      options: () => [["", "（跟随主Agent）"], ...Object.entries(AVAILABLE_MODELS).map(([k, v]) => [k, v])],
      get(n) { return this._lpGet(n, "model"); }, set(n, v) { this._lpSet(n, "model", v); } },
    { key: "systemPrompt", label: "systemPrompt", widget: "textarea",
      tip: "角色/格式约束；声明了输出结构时会自动并入 JSON Schema",
      get(n) { return this._lpGet(n, "systemPrompt"); }, set(n, v) { this._lpSet(n, "systemPrompt", v); } },
    { key: "prompt", label: "prompt", widget: "textarea", canvas: true,
      tip: "{{输入字段名}} 占位符引用上游；画布上可直接编辑",
      get(n) { return this._lpGet(n, "prompt"); }, set(n, v) { this._lpSet(n, "prompt", v); } },
    { key: "output_format", label: "输出格式", widget: "select",
      options: [["json", "JSON（结构化解析）"], ["text", "TEXT（原文透传）"]],
      hint: n => (this._lpGet(n, "output_format") || "json") === "text"
        ? "TEXT：不并入 schema 约束、不解析；content 原文走 output 端口" : "",
      get(n) { return this._lpGet(n, "output_format") || "json"; }, set(n, v) { this._lpSet(n, "output_format", v); } },
    { key: "thinking", label: "thinking", widget: "select",
      options: [["", "（跟随默认）"], ["true", "开"], ["false", "关"]],
      get(n) { return this._lpGet(n, "thinking"); }, set(n, v) { this._lpSet(n, "thinking", v); } },
    { key: "timeout", label: "超时(秒)", widget: "number", tip: "留空=全局默认",
      get(n) { return this._lpGet(n, "timeout"); }, set(n, v) { this._lpSet(n, "timeout", v); } },
    { key: "onError", label: "失败输出", widget: "input", tip: "失败时输出此文本（留空=中断工作流）",
      get(n) { return this._lpGet(n, "onError"); }, set(n, v) { this._lpSet(n, "onError", v); } },
  ],
});
