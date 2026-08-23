// HTTP 请求节点插件（type 45）：方法/URL/请求体/超时。
// params：method/url/timeout 走通用 widget；body 按 bodyType 联动出 JSON/表单/纯文本编辑器（custom）。
EdFW.register({
  type: "45", label: "HTTP",     icon: "🌐", category: "io",
  section: "HTTP 请求",
  params: [
    { key: "method", label: "方法", widget: "select", options: ["GET", "POST", "PUT", "DELETE"],
      get(n) { return n.data.inputs?.apiInfo?.method || "GET"; },
      set(n, v) { let a = n.data.inputs.apiInfo || (n.data.inputs.apiInfo = {}); a.method = v; if (v === "GET") { const b = n.data.inputs.body || (n.data.inputs.body = {}); b.bodyType = "EMPTY"; } } },
    { key: "url", label: "URL", widget: "input", tip: "https://... 支持 {{字段名}} 占位符",
      get(n) { return n.data.inputs?.apiInfo?.url || ""; },
      set(n, v) { let a = n.data.inputs.apiInfo || (n.data.inputs.apiInfo = {}); a.url = v; } },
    { key: "body", label: "请求体", widget: "custom",
      get(n) { const bd = n.data.inputs?.body || {}; return bd.bodyType || "EMPTY"; }, set() {},
      html(n) {
        const api = n.data.inputs?.apiInfo || {};
        if (api.method !== "POST" && api.method !== "PUT") return "";
        const bd = n.data.inputs?.body || {};
        let h = `<label>请求体类型 <select onchange="NODEP_HTTP_set('bodyType',this.value)">` +
                ["EMPTY|无", "JSON|JSON", "FORM_URLENCODED|表单(URL编码)", "RAW_TEXT|纯文本"]
                  .map(s => { const [v, t] = s.split("|"); return `<option value="${v}"${(bd.bodyType || "EMPTY") === v ? " selected" : ""}>${t}</option>`; }).join("") +
                `</select></label>`;
        if (bd.bodyType === "JSON") {
          h += `<label>JSON 体 <textarea onchange="NODEP_HTTP_set('bodyJson',this.value)" style="min-height:80px">${ext((bd.bodyData || {}).json)}</textarea></label>` +
               `<div style="font-size:10px;color:#8a9099">支持 {{输入字段名}} 占位符引用上游数据</div>`;
        }
        if (bd.bodyType === "RAW_TEXT") {
          h += `<label>文本体 <textarea onchange="NODEP_HTTP_set('bodyText',this.value)" style="min-height:60px">${ext((bd.bodyData || {}).rawText)}</textarea></label>`;
        }
        if (bd.bodyType === "FORM_URLENCODED") {
          h += `<label>表单字段</label>`;
          const fields = (bd.bodyData || {}).formURLEncoded || [];
          fields.forEach((p, pi) => {
            h += `<div style="display:flex;gap:3px;margin:2px 0">` +
                 `<input value="${ext(p.name)}" placeholder="键" onchange="NODEP_HTTP_form(${pi},'name',this.value)" style="flex:1;font-size:10px">` +
                 `<input value="${ext(p.input?.value?.content)}" placeholder="值" onchange="NODEP_HTTP_form(${pi},'val',this.value)" style="flex:2;font-size:10px">` +
                 `<button class="del" onclick="NODEP_HTTP_formDel(${pi})">×</button></div>`;
          });
          h += `<button onclick="NODEP_HTTP_formAdd()" style="font-size:10px">+ 字段</button>`;
        }
        return h;
      } },
    { key: "timeout", label: "超时(秒)", widget: "number",
      get(n) { return (n.data.inputs?.setting || {}).timeout ?? 15; },
      set(n, v) { let s = n.data.inputs.setting || (n.data.inputs.setting = {}); s.timeout = parseInt(v) || 15; } },
  ],
});
// HTTP 编辑器全局
function NODEP_HTTP_set(field, v) {
  const n = findN(selNode); const d = n.data.inputs;
  if (field === "method") { let a = d.apiInfo || (d.apiInfo = {}); a.method = v; if (v === "GET") { const b = d.body || (d.body = {}); b.bodyType = "EMPTY"; } }
  else if (field === "url") { let a = d.apiInfo || (d.apiInfo = {}); a.url = v; }
  else if (field === "bodyType") { let b = d.body || (d.body = {}); b.bodyType = v; if (v === "JSON") { b.bodyData = b.bodyData || {}; b.bodyData.json = b.bodyData.json || ""; } if (v === "RAW_TEXT") { b.bodyData = b.bodyData || {}; b.bodyData.rawText = b.bodyData.rawText || ""; } if (v === "FORM_URLENCODED") { b.bodyData = b.bodyData || {}; b.bodyData.formURLEncoded = b.bodyData.formURLEncoded || []; } }
  else if (field === "bodyJson") { let b = d.body || (d.body = {}); b.bodyData = b.bodyData || {}; b.bodyData.json = v; }
  else if (field === "bodyText") { let b = d.body || (d.body = {}); b.bodyData = b.bodyData || {}; b.bodyData.rawText = v; }
  else if (field === "timeout") { let s = d.setting || (d.setting = {}); s.timeout = parseInt(v) || 15; }
  showProps(); renderAll();
}
function NODEP_HTTP_form(pi, field, v) {
  const n = findN(selNode); const bd = (n.data.inputs.body || (n.data.inputs.body = {}));
  bd.bodyData = bd.bodyData || {}; bd.bodyData.formURLEncoded = bd.bodyData.formURLEncoded || [];
  const p = bd.bodyData.formURLEncoded[pi]; if (!p) return;
  if (field === "name") p.name = v;
  else { p.input = p.input || { type: "string", value: { type: "literal", content: "" } }; p.input.value.content = v; }
}
function NODEP_HTTP_formAdd() {
  const n = findN(selNode); const bd = (n.data.inputs.body || (n.data.inputs.body = {}));
  bd.bodyData = bd.bodyData || {}; bd.bodyData.formURLEncoded = bd.bodyData.formURLEncoded || [];
  bd.bodyData.formURLEncoded.push({ name: "", input: { type: "string", value: { type: "literal", content: "" } } });
  showProps(); renderAll();
}
function NODEP_HTTP_formDel(pi) {
  const n = findN(selNode); const bd = n.data.inputs.body || {};
  if (bd.bodyData?.formURLEncoded) bd.bodyData.formURLEncoded.splice(pi, 1);
  showProps(); renderAll();
}
