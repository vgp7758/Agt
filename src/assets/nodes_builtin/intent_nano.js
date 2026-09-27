// Intent·Nano 节点插件（type intent_nano）：NanoJev 判别式意图路由（内置 Intent 加强版）。
// intents 双来源：ref 动态列表（inputParameters.intents）或 XML 静态子元素；threshold 数字控件。
// 出口 branch_N/default 与内置 intent 同构（workflow_editor 的端口生成条件含 intent_nano）。
EdFW.register({
  type: "intent_nano", label: "Intent·Nano",    icon: "🧭", category: "llm",
  section: "意图选项（name + 语义描述；判别式路由 branch_N）",
  defaults: {nodeMeta: {title: "意图路由·Nano"},
             inputs: {inputParameters: [
                        {name: "query", input: {type: "string", value: {type: "literal", content: ""}}},
                        {name: "intents", input: {type: "list", value: {type: "literal", content: ""}}},
                        {name: "route", input: {type: "boolean", value: {type: "literal", content: "true"}}},
                        {name: "threshold", input: {type: "number", value: {type: "literal", content: "0.35"}}}],
                      intents: [{name: "code", description: "要求编写、修改、调试代码"},
                                {name: "search", description: "要求检索资料、查文档、搜索信息"},
                                {name: "chat", description: "闲聊、打招呼或常识问答"}]},
             outputs: [{name: "intent", type: "string", description: "top1 意图名（低于阈值为空）", fixed: true},
                       {name: "confidence", type: "number", description: "top1 置信度", fixed: true},
                       {name: "probabilities", type: "object", description: "完整概率分布", fixed: true}]},
  params: [
    // widget 契约（编辑器 switch）：custom/branches/groups/textarea/code/number/checkbox/select，
    // 无 'input'（普通文本走 default）；占位符 key 是 tip（非 ph）——用户实测 2026-09-22
    { key: "query", label: "query", tip: "待分类文本（ref 上游字段）",
      get(n) { return ipGet(n, "query"); }, set(n, v) { ipSet(n, "query", v); } },
    { key: "intents", label: "意图列表（动态/ref）", widget: "textarea",
      tip: "★ref 连线优先（type=list，如上游技能清单 [{name,description}]）；或手填 JSON 数组。留空=用下方静态意图编辑",
      get(n) { const p = (n.data.inputs?.inputParameters || []).find(x => x.name === "intents");
               if (p?.input?.value?.type === "ref") { const c = p.input.value.content || {}; return "🔗 " + (c.blockID || "?") + "." + (c.name || "?"); }
               return p?.input?.value?.content ?? ""; },
      set(n, v) { ipSet(n, "intents", v, "list"); } },
    { key: "route", label: "branch 路由", widget: "checkbox",
      tip: "true=branch_N/default 端口路由（须按意图接端口边）；false=单出口（port=None，直连边照走——动态意图列表/只取 outputs 用）",
      get(n) { const v = ipGet(n, "route"); return v === "" || v === true || String(v).toLowerCase() !== "false"; },
      set(n, v) { ipSet(n, "route", v, "boolean"); } },
    { key: "threshold", label: "阈值", widget: "number", tip: "0.35（top1 低于它走 default）",
      get(n) { return ipGet(n, "threshold"); }, set(n, v) { ipSet(n, "threshold", v); } },
    { key: "intents_static", label: "静态意图（XML 子元素）", widget: "custom",
      get(n) { return (n.data.inputs?.intents || []).map(x => x.name).join(","); },
      set() {},
      html(n) {
        const intents = n.data.inputs?.intents || [];
        let h = '<div style="font-size:10px;color:#8a9099;margin-bottom:2px">静态 <intent> 子元素（intents 参数留空时才生效）：</div>';
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
    // 画布体契约 = 原生 DOM（elm + g.appendChild）——不是 d3 链式！
    // 此前误用 g.append("text").attr(...)：DOM 的 append() 返回 undefined → .attr 炸
    // 「Cannot read properties of undefined (reading 'attr')」（用户实锤 2026-09-22 建节点即报）
    const intents = n.data.inputs?.intents || [];
    const yBase = HDR_H + Math.max(nodeInputs(n).length, nodeOutputs(n).length) * ROW_H + 4;
    intents.forEach((it, i) => {
      g.appendChild(elm('text', {x: 10, y: yBase + 10 + i * 14, class: 'sub-row',
                                 fill: it.description ? '#475569' : '#b6bcc4'},
        `${i + 1}. ${it.name || '…'}${it.description ? ' — ' + String(it.description).slice(0, 18) : '（无描述）'}`));
    });
  },
});
function ipGet(n, k) {
  const lp = n.data.inputs?.inputParameters || [];
  return lp.find(x => x.name === k)?.input?.value?.content ?? "";
}
function ipSet(n, k, v, type) {
  let lp = n.data.inputs.inputParameters || (n.data.inputs.inputParameters = []);
  let p = lp.find(x => x.name === k);
  if (!p) { p = { name: k, input: { type: type || "string", value: { type: "literal", content: "" } } }; lp.push(p); }
  if (type) p.input.type = type;   // 类型变更（string→list/boolean：端口类型与 ref 匹配）
  p.input.value.content = v;
}
// 意图编辑器全局（注入脚本=全局作用域；name/description 双列）
function NODEP_INANO_set(i, k, v) { const n = findN(selNode); n.data.inputs.intents[i][k] = v; renderAll(); }
function NODEP_INANO_add()   { const n = findN(selNode); (n.data.inputs.intents = n.data.inputs.intents || []).push({ name: '新意图', description: '' }); showProps(); renderAll(); }
function NODEP_INANO_del(i)  { const n = findN(selNode); n.data.inputs.intents.splice(i, 1); showProps(); renderAll(); }
