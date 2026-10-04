// ===== i18n 双向引擎（共享版，2026-10-04）——各页面 <script src="/i18n/engine.js"> 引入 =====
// 字典：src/static/i18n/dict.json（中文 key → 英文 value，一份文件两种方向都能查）
// 语言链：?lang= > localStorage.agt_lang > navigator.language > 默认 zh
// 切换即 reload（简单可靠）；缺翻译自动 fallback 原文（双语混排过渡期安全）
let _I18N = { lang: null, zh2en: null, en2zh: null, ready: false };
function _i18nLang(){
  const q = new URLSearchParams(location.search).get('lang');
  if (q) return q;
  const ls = localStorage.getItem('agt_lang');
  if (ls) return ls;
  return (navigator.language||'').toLowerCase().startsWith('en') ? 'en' : 'zh';
}
async function i18nInit(){
  _I18N.lang = _i18nLang();
  if (_I18N.lang !== 'en' && _I18N.lang !== 'bi') { document.documentElement.lang='zh-CN'; return; }
  document.documentElement.lang = _I18N.lang==='en' ? 'en' : 'zh-CN';
  try {
    const r = await fetch('/api/i18n', {cache:'no-store'});
    if (!r.ok) return;
    _I18N.zh2en = await r.json();
    _I18N.en2zh = {};
    for (const k in _I18N.zh2en) { const v = _I18N.zh2en[k]; if (v) _I18N.en2zh[v] = k; }
    _I18N.ready = true;
    applyI18n(document);
    _i18nObserve();
  } catch(e) { /* 字典取不到 = 保持中文 */ }
}
function _i18nHit(s){
  if (!_I18N.ready || !s) return null;
  const t = s.trim();
  if (!t) return null;
  if (_I18N.lang === 'bi'){          // 双语展示（2026-10-04 用户裁定）：中文主 + 英文副
    const en = _I18N.zh2en[t];
    return (en && !s.includes(en)) ? (s.replace(t, t + ' ' + en)) : null;
  }
  const hit = _I18N.lang==='en' ? (_I18N.zh2en[t] || null) : (_I18N.en2zh[t] || null);
  return hit == null ? null : (s.replace(t, hit));
}
function _i18nAttrs(el){
  for (const a of ['title','placeholder','aria-label','data-label']){
    const v = el.getAttribute && el.getAttribute(a);
    if (!v) continue;
    const hit = _i18nHit(v);
    if (hit != null) el.setAttribute(a, hit);
  }
}
function applyI18n(root){
  if (!_I18N.ready || !root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  let n;
  while ((n = walker.nextNode())) texts.push(n);
  for (const node of texts){
    const hit = _i18nHit(node.nodeValue);
    if (hit != null) node.nodeValue = hit;
  }
  // 属性扫描：root 自身 + 子元素（叶文本元素的 title 常挂在自己上）
  if (root.nodeType === 1) _i18nAttrs(root);
  root.querySelectorAll && root.querySelectorAll('*').forEach(_i18nAttrs);
}
let _i18nT = null;
function _i18nObserve(){
  if (_i18nT) return;
  const mo = new MutationObserver(() => {
    clearTimeout(_i18nT);
    _i18nT = setTimeout(() => applyI18n(document.body), 150);
  });
  mo.observe(document.body, {childList:true, subtree:true, characterData:false});
}
function _t(s, params){
  let out = s;
  if (_I18N.lang==='en' && _I18N.zh2en) out = (_I18N.zh2en[s] || s);
  else if (_I18N.lang==='zh' && _I18N.en2zh) out = (_I18N.en2zh[s] || s);
  else if (_I18N.lang==='bi' && _I18N.zh2en && _I18N.zh2en[s]) out = s + ' ' + _I18N.zh2en[s];
  if (params) for (const k in params) out = out.replace(new RegExp('\\{'+k+'\\}','g'), params[k]);
  return out;
}
function setLang(l){
  localStorage.setItem('agt_lang', l);
  const u = new URL(location.href); u.searchParams.set('lang', l); location.href = u.href;
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', i18nInit);
else i18nInit();
