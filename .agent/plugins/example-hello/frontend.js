/* example-hello —— 插件前端入口（AgtPlugin SDK 演示：按钮 + 面板 + 聊天卡片 + 事件订阅）。
 * 页面启动时由 index.html 注入器加载（/plugins-assets/example-hello/frontend.js，no-cache）。
 * 改完刷新页面即生效。全局 AgtPlugin API 见 index.html 尾部「插件前端 SDK」。
 */
(function () {
  // ① 聊天卡片：后端 emit({"type": "example_hello_event", ...}) → 自绘卡片
  AgtPlugin.ui.addChatCard('example_hello_event', (b, m) => {
    b.innerHTML =
      '<div style="border:1px solid #a7f3d0;background:#ecfdf5;border-radius:8px;padding:8px 12px">' +
      '<div style="font-weight:600;color:#065f46">🔌 ' + AgtPlugin.util.esc(m.title || '插件事件') + '</div>' +
      '<div style="color:#064e3b;font-size:13px;margin-top:4px">' + AgtPlugin.util.esc(m.detail || '') + '</div>' +
      '</div>';
  });

  // ② 侧滑面板：点开时调插件路由 POST /api/example-hello/ping 显示统计
  const panel = AgtPlugin.ui.addPanel({
    icon: '🔌', title: 'example-hello 面板', background: '#0f766e',
    render: async (el) => {
      el.innerHTML = '<div style="color:#6b7280">请求插件路由 ping 中…</div>';
      try {
        const r = await AgtPlugin.api.post('/api/example-hello/ping', {});
        el.innerHTML =
          '<div style="font-size:13px;line-height:1.9">' +
          '<b>pong:</b> ' + r.pong + '（插件路由 POST /api/example-hello/ping）<br>' +
          '<b>工具调用观察:</b> ' + (r.stats ? r.stats.tool_calls : '?') + ' 次<br>' +
          '<b>拦截:</b> ' + (r.stats ? r.stats.denied : '?') + ' 次<br>' +
          '<b>招呼名单:</b> ' + AgtPlugin.util.esc(((r.stats && r.stats.greeted) || []).join('、') || '（空）') +
          '</div>';
      } catch (e) {
        el.innerHTML = '<div style="color:#dc2626">请求失败：' + AgtPlugin.util.esc(String(e)) +
          '<br>（插件路由需 server 已启动且后端已装配）</div>';
      }
    },
  });

  // ③ dock 按钮：toast + 打开面板
  AgtPlugin.ui.addButton({
    icon: '👋', label: '插件打招呼', background: '#0f766e',
    onClick: () => { AgtPlugin.util.toast('👋 来自插件 example-hello 的问候！'); panel.toggle(); },
  });

  // ④ 事件订阅（不渲染，仅观察）：answer 事件到达时 console 记录
  AgtPlugin.ws.on('answer', (m) => {
    console.info('[example-hello] 观察到一条 answer，长度', (m.text || '').length);
  });

  console.info('[example-hello] 前端已装配（卡片1/面板1/按钮1/订阅1）');
})();
