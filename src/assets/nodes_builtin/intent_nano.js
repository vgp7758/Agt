// Intent·Nano 节点插件（type intent_nano）：NanoJev 判别式意图路由（内置 Intent 加强版）。
// intents 编辑器 = name + description 两列（description 直供模型判别）；temperature/threshold 数字控件。
// 出口 branch_N/default 与内置 intent 同构（workflow_editor 的端口生成条件含 intent_nano）。
EdFW.register({
  type: "intent_nano", label: "Intent·Nano",    icon: "🧭", category: "llm",
  section: "意图选项（name + 语义描述；判别式路由 branch_N）",
  defaults: {nodeMeta: {title: "意图路由·Nano"},
             inputs: {inputParameters: [
                        {name: "query", input: {type: "string", value: {type: "literal", content: ""}}},
                        {name: "temperature", input: {type: "number", value: {type: "literal", content: "1.0"}}},
                        {name: "threshold", input: {type: "number", value: {type: "literal", content: "0.35"}}}],
                      intents: [{name: "code", description: "要求编写、修改、调试代码"},
                                {name: "search", description: "要求检索资料、查文档、搜索信息"},
                                {name: "chat", description: "闲聊、打招呼或常识问答"}]},
             outputs: [{name: "intent", type: "string", description: "top1 意图名（低于阈值为空）", fixed: true},
                       {name: "confidence", type: "number", description: "top1 置信度", fixed: true},
                       {name: "probabilities", type: "object", description: "完整概率分布", fixed: true}]},
  params: [
    { key: "query", label: "query", widget: "input", ph: "待分类文本（ref 上游字段）",
      get(n) { return ipGet(n, "query"); }, set(n, v) { ipSet(n, "query", v); } },
    { key: "temperature", label: "温度", widget: "input", ph: "1.0",
      get(n) { return ipGet(n, "temperature"); }, set(n, v) { ipSet(n, "temperature", v); } },
    { key: "threshold", label: "阈值", widget: "input", ph: "0.35（top1 低于它走 default）",
      get(n) { return ipGet(n, "threshold"); }, set(n, v) { ipSet(n, "threshold", v); } },
    { key: "intents", label: "意图", widget: "custom",
      get(n) { return (n.data.inputs?.intents || []).map(x => x.name).join(","); },
      set() {},
      html(n) {
        const intents = n.data.inputs?.intents || [];
        let h = '';
        intents.forEach((it, i) => {
          h += `<div style="display:flex;gap:3px;margin:2px 0;align-items:center"><span style="font-size:10px;color:#8a9099">${i + 1}.</span>` +
               `<input value="${ext(it.name || '')}" placeholder="意图名" onchange="NODEP_INANO_set(${i},'name',this.value)" style="width:80px;font-size:11px">` +
               `<input value="${ext(it.description || '')}" placeholder="语义描述（供 NanoJev 判别）" onchange="NODEP_INANO_set(${i},'description',this.value)" style="flex:1;font-size:11px">` +
               `<button class="del" onclick="NODEP_INANO_del(${i})">×</button></div>`;
        });
        h += `<button onclick="NODEP_INANO_add()">+ 意图</button>`;
        h += `<div style="font-size:10px;color:#8a9099;margin-top:4px">NanoJev 判别式（本地 ~1s 零 token）：输出概率分布，top1 低于阈值走 default。出口与内置 Intent 同构。</div>`;
        return h;
      } },
  ],
  nodeH(n) { return Math.max(3, (n.data.inputs?.intents || []).length) * 14 + 20; },
  body(n, g) {
    const intents = n.data.inputs?.intents || [];
    const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
    intents.forEach((it, i) => {
      g.append("text").attr("x", 10).attr("y", yBase + 10 + i * 14)
        .attr("font-size", 9).attr("fill", it.description ? "#475569" : "#b6bcc4")
        .text(`${i + 1}. ${it.name || "…"}${it.description ? " — " + String(it.description).slice(0, 18) : "（无描述）"}`);
    });
  },
});
function ipGet(n, k) {
  const lp = n.data.inputs?.inputParameters || [];
  return lp.find(x => x.name === k)?.input?.value?.content ?? "";
}
function ipSet(n, k, v) {
  let lp = n.data.inputs.inputParameters || (n.data.inputs.inputParameters = []);
  let p = lp.find(x => x.name === k);
  if (!p) { p = { name: k, input: { type: "string", value: { type: "literal", content: "" } } }; lp.push(p); }
  p.input.value.content = v;
}
// 意图编辑器全局（注入脚本=全局作用域；name/description 双列）
function NODEP_INANO_set(i, k, v) { const n = findN(selNode); n.data.inputs.intents[i][k] = v; renderAll(); }
function NODEP_INANO_add()   { const n = findN(selNode); (n.data.inputs.intents = n.data.inputs.intents || []).push({ name: '新意图', description: '' }); showProps(); renderAll(); }
function NODEP_INANO_del(i)  { const n = findN(selNode); n.data.inputs.intents.splice(i, 1); showProps(); renderAll(); }
