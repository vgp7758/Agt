/* open-folder —— 插件前端：dock 按钮 📁（原 index.html 内置 btnOpenDir 插件化，2026-10-15）。
 * 点击 POST 插件路由 /api/open-folder/open → 在跑 agt 的机器上弹文件管理器
 * （从手机访问时弹的也是电脑上的文件夹——正是常用场景）。 */
(function () {
  AgtPlugin.ui.addButton({
    icon: '📁', label: '打开文件夹', background: '#ca8a04',
    onClick: async () => {
      try {
        const d = await AgtPlugin.api.post('/api/open-folder/open', {});
        if (d.ok) AgtPlugin.util.toast('📁 已打开工作区文件夹（在跑 agt 的那台机器上）');
        else AgtPlugin.util.toast('❌ ' + (d.error || '打开失败'));
      } catch (e) {
        AgtPlugin.util.toast('❌ 打开失败：' + e.message);
      }
    },
  });
  console.info('[open-folder] 前端已装配（按钮1）');
})();
