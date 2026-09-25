// Jev·批量判断 节点插件（type jev_batch_judge）：长文本按行分组 → NanoJev 批量命题判断 → 聚合。
// questions 编辑器 = 题名 + 命题文本两列（boolean 命题；事实陈述式 state 由调用方保证质量）。
// 聚合 agg(max/min/avg select) × early_threshold(0=全量并行 / 非0=逐组早停)。
EdFW.register({
  type: "jev_batch_judge", label: "Jev·批量判断",    icon: "⚖️", category: "llm",
  section: "判断题（题名 + 命题文本；每组 state 并行判断后聚合）",
  defaults: {nodeMeta: {title: "Jev批量判断"},
             inputs: {inputParameters: [
                        {name: "text", input: {type: "string", value: {type: "literal", content: ""}}},
                        {name: "group_max_chars", input: {type: "number", value: {type: "literal", content: "600"}}},
                        {name: "agg", input: {type: "string", value: {type: "literal", content: "max"}}},
                        {name: "early_threshold", input: {type: "number", value: {type: "literal", content: "0"}}},
                        {name: "temperature", input: {type: "number", value: {type: "literal", content: "1.0"}}}],
                      questions: [{name: "敏感", text: "文本中出现了敏感词。"},
                                  {name: "代码", text: "文本中包含代码。"}]},
             outputs: [{name: "results", type: "object", description: "{题名:{agg,value,per_group[],stopped_early}}", fixed: true},
                       {name: "max_agg", type: "number", description: "所有题中最大聚合值", fixed: true},
                       {name: "hit", type: "string", description: "早停命中的题名（全量时空）", fixed: true},
                       {name: "raw", type: "string", description: "分组元信息 JSON", fixed: true}]},
  params: [
    { key: "text", label: "text", tip: "待判断长文本（ref 上游字段）",
      get(n) { return ipGet2(n, "text"); }, set(n, v) { ipSet2(n, "text", v, "string"); } },
    { key: "group_max_chars", label: "组上限", widget: "number", tip: "600（每组最大字符；英文可 ~1500）",
      get(n) { return ipGet2(n, "group_max_chars"); }, set(n, v) { ipSet2(n, "group_max_chars", v, "number"); } },
    { key: "agg", label: "聚合", widget: "select",
      options: [["max", "最大值"], ["min", "最小值"], ["avg", "平均值"]], tip: "各组 p_true 的聚合函数",
      get(n) { return ipGet2(n, "agg") || "max"; }, set(n, v) { ipSet2(n, "agg", v, "string"); } },
    { key: "early_threshold", label: "早停阈值", widget: "number", tip: "0=全量并行；非0=逐组推理，中途聚合≥它即停",
      get(n) { return ipGet2(n, "early_threshold"); }, set(n, v) { ipSet2(n, "early_threshold", v, "number"); } },
    { key: "temperature", label: "温度", widget: "number", tip: "1.0",
      get(n) { return ipGet2(n, "temperature"); }, set(n, v) { ipSet2(n, "temperature", v, "number"); } },
    { key: "questions", label: "判断题", widget: "custom",
      get(n) { return (n.data.inputs?.questions || []).map(x => x.name).join(","); },
      set() {},
      html(n) {
        const qs = n.data.inputs?.questions || [];
        let h = '';
        qs.forEach((q, i) => {
          h += `<div style="display:flex;gap:3px;margin:2px 0;align-items:center"><span style="font-size:10px;color:#8a9099">${i + 1}.</span>` +
               `<input value="${ext(q.name || '')}" placeholder="题名" onchange="NODEP_JBJ_set(${i},'name',this.value)" style="width:70px;font-size:11px">` +
               `<input value="${ext(q.text || '')}" placeholder="命题文本（事实陈述式，如：文本中出现了敏感词。）" onchange="NODEP_JBJ_set(${i},'text',this.value)" style="flex:1;font-size:11px">` +
               `<button class="del" onclick="NODEP_JBJ_del(${i})">×</button></div>`;
        });
        h += `<button onclick="NODEP_JBJ_add()">+ 判断题</button>`;
        h += `<div style="font-size:10px;color:#8a9099;margin-top:4px">切片=按行顺序贪心装组（无 LLM）；每组一个 state 送 NanoJev（512 tok 硬限）。聚合 ${['max','min','avg'].join('/')} × 早停/全量。</div>`;
        return h;
      } },
  ],
  nodeH(n) { return Math.max(3, (n.data.inputs?.questions || []).length) * 14 + 20; },
  body(n, g) {
    const qs = n.data.inputs?.questions || [];
    const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
    const agg = ipGet2(n, "agg") || "max";
    const early = ipGet2(n, "early_threshold") || "0";
    g.appendChild(elm('text', {x: 10, y: yBase + 10, class: 'sub-row', fill: '#475569'},
      `聚合 ${agg} · ${parseFloat(early) > 0 ? '早停 ≥' + early : '全量'}`));
    qs.forEach((q, i) => {
      g.appendChild(elm('text', {x: 10, y: yBase + 24 + i * 14, class: 'sub-row',
                                 fill: q.text ? '#475569' : '#b6bcc4'},
        `${i + 1}. ${q.name || '…'}${q.text ? ' — ' + String(q.text).slice(0, 16) : '（无命题）'}`));
    });
  },
});
function ipGet2(n, k) {
  const lp = n.data.inputs?.inputParameters || [];
  return lp.find(x => x.name === k)?.input?.value?.content ?? "";
}
function ipSet2(n, k, v, typ) {
  let lp = n.data.inputs.inputParameters || (n.data.inputs.inputParameters = []);
  let p = lp.find(x => x.name === k);
  if (!p) { p = { name: k, input: { type: typ || "string", value: { type: "literal", content: "" } } }; lp.push(p); }
  p.input.type = typ || "string";
  p.input.value.content = v;
}
function NODEP_JBJ_set(i, k, v) { const n = findN(selNode); n.data.inputs.questions[i][k] = v; renderAll(); }
function NODEP_JBJ_add()   { const n = findN(selNode); (n.data.inputs.questions = n.data.inputs.questions || []).push({ name: '新题', text: '' }); showProps(); renderAll(); }
function NODEP_JBJ_del(i)  { const n = findN(selNode); n.data.inputs.questions.splice(i, 1); showProps(); renderAll(); }
