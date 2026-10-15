/* feedback —— 插件前端：dock 💬 反馈面板（原 index.html 内置弹窗插件化，2026-10-15）。
 * addPanel 侧滑表单：类型/内容/联系方式/附环境；提交走插件路由 POST /api/feedback/submit。 */
(function () {
  AgtPlugin.ui.addPanel({
    icon: '💬', title: '提交反馈', background: '#16a34a',
    render: async (el) => {
      el.innerHTML = '<div style="color:#6b7280">加载反馈元信息…</div>';
      let meta = {};
      try { meta = await AgtPlugin.api.post('/api/feedback/meta', {}); } catch (e) {}
      const kinds = meta.kinds || ['bug', '建议', '问题', '赞美'];
      const envPrev = meta.env ? Object.entries(meta.env).map(([k, v]) => k + '=' + v).join(' · ') : '';
      const contact = meta.contact || '';
      el.innerHTML =
        '<div style="font-size:13px;line-height:1.7;display:flex;flex-direction:column;gap:8px">' +
        '<label>类型 <select id="fbpKind" style="padding:4px 6px;border:1px solid #d1d5db;border-radius:6px;font-size:13px">' +
        kinds.map(k => `<option value="${AgtPlugin.util.esc(k)}">${AgtPlugin.util.esc(k)}</option>`).join('') +
        '</select></label>' +
        '<textarea id="fbpContent" rows="4" placeholder="说说你遇到的问题、建议或感受…" style="width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical;outline:none"></textarea>' +
        '<label>联系方式（可选，方便作者回你）' +
        '<input id="fbpContact" type="text" placeholder="微信/邮箱" style="width:160px;padding:4px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:13px"></label>' +
        '<label style="display:flex;align-items:center;gap:6px"><input id="fbpEnv" type="checkbox" checked> 附上环境信息（便于定位问题）</label>' +
        (envPrev ? `<div style="font-size:11px;color:#8a9099">将附上：${AgtPlugin.util.esc(envPrev)}</div>` : '') +
        (contact ? `<div style="font-size:12px;color:#5b6470;white-space:pre-wrap">直接联系作者：${AgtPlugin.util.esc(contact)}</div>` : '') +
        '<div><button id="fbpSubmit" style="background:#16a34a;color:#fff;border:none;padding:6px 20px;border-radius:6px;cursor:pointer;font-size:13.5px">提交反馈</button></div>' +
        '</div>';
      el.querySelector('#fbpSubmit').onclick = async () => {
        const content = el.querySelector('#fbpContent').value.trim();
        if (!content) { AgtPlugin.util.toast('⚠️ 请先写点内容'); return; }
        try {
          const r = await AgtPlugin.api.post('/api/feedback/submit', {
            kind: el.querySelector('#fbpKind').value, content,
            contact: el.querySelector('#fbpContact').value.trim(),
            include_env: el.querySelector('#fbpEnv').checked,
          });
          AgtPlugin.util.toast(r.text || (r.ok ? '✅ 已提交' : '❌ 提交失败'));
          if (r.ok) el.querySelector('#fbpContent').value = '';
        } catch (e) {
          AgtPlugin.util.toast('❌ 提交失败：' + e.message);
        }
      };
      setTimeout(() => el.querySelector('#fbpContent').focus(), 50);
    },
  });
  console.info('[feedback] 前端已装配（面板1）');
})();
