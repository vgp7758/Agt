// Intent 意图节点插件（type 22）：LLM 意图分类路由。
// params：model（llmParam）+ intents（custom 编辑器：增删改意图名）。
EdFW.register({
  type: "22", label: "Intent",    icon: "🎯", category: "llm",
  section: "意图选项（每项一个分支出口）",
  _lpGet(n, k) {
    const lp = n.data.inputs?.llmParam || [];
    return lp.find(x => x.name === k)?.input?.value?.content ?? "";
  },
  _lpSet(n, k, v) {
    let lp = n.data.inputs.llmParam || (n.data.inputs.llmParam = []);
    let p = lp.find(x => x.name === k);
    if (!p) { p = { name: k, input: { type: 'string', value: { type: 'literal', content: '' } } }; lp.push(p); }
    p.input.value.content = v;
  },
  params: [
    { key: "model", label: "模型", widget: "select",
      options: () => [["", "（跟随主Agent）"], ...Object.entries(AVAILABLE_MODELS).map(([k, v]) => [k, v])],
      get(n) { return this._lpGet(n, "model"); }, set(n, v) { this._lpSet(n, "model", v); } },
    { key: "intents", label: "意图", widget: "custom",
      get(n) { return (n.data.inputs?.intents || []).map(x => x.name).join(","); },
      set() {},
      html(n) {
        const intents = n.data.inputs?.intents || [];
        let h = '';
        intents.forEach((it, i) => {
          h += `<div style="display:flex;gap:3px;margin:2px 0;align-items:center"><span style="font-size:10px;color:#8a9099">${i + 1}.</span>` +
               `<input value="${ext(it.name)}" placeholder="意图名" onchange="NODEP_INTENT_set(${i},this.value)" style="flex:1;font-size:11px">` +
               `<button class="del" onclick="NODEP_INTENT_del(${i})">×</button></div>`;
        });
        h += `<button onclick="NODEP_INTENT_add()">+ 意图</button>`;
        h += `<div style="font-size:10px;color:#8a9099;margin-top:4px">LLM 分类后路由到对应分支(branch_N)，未命中走默认。每意图在画布右侧有一个流程出口。</div>`;
        return h;
      } },
  ],
});
// 意图编辑器全局（本插件自定义控件用；注入脚本=全局作用域）
function NODEP_INTENT_set(i, v) { const n = findN(selNode); n.data.inputs.intents[i].name = v; renderAll(); }
function NODEP_INTENT_add()   { const n = findN(selNode); (n.data.inputs.intents = n.data.inputs.intents || []).push({ name: '新意图' }); showProps(); renderAll(); }
function NODEP_INTENT_del(i)  { const n = findN(selNode); n.data.inputs.intents.splice(i, 1); showProps(); renderAll(); }
