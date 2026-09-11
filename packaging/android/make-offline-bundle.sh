#!/data/data/com.termux/files/usr/bin/sh
# 离线母本打包：在【已装好 agt 的 Termux】里跑，产出给别人免编译免下载的安装包。
# 用法：sh make-offline-bundle.sh   →  ~/agt-offline-<日期>.tar.gz + restore-offline.sh
set -e
echo "==> 定位 site-packages"
SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
echo "    $SITE"
VER=$(pip show agt-agent 2>/dev/null | grep -i ^Version | awk '{print $2}')
[ -n "$VER" ] || { echo "❌ 本机未安装 agt-agent（先跑 install.sh）"; exit 1; }
STAMP=$(date +%Y%m%d)
PKG="$HOME/agt-offline-$VER-$STAMP"
mkdir -p "$PKG"

echo "==> 打包 site-packages（约几百 MB，稍等）"
tar -czf "$PKG/site-packages.tgz" -C "$(dirname "$SITE")" "$(basename "$SITE")"

echo "==> 生成 restore-offline.sh（目标机执行它完成安装）"
cat > "$PKG/restore-offline.sh" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# 目标机使用：装好 Termux + 把整个 agt-offline-* 文件夹放进手机存储后，在 Termux 里执行
#   termux-setup-storage   （一次性授权，已授权过可跳过）
#   cd /sdcard/Download/agt-offline-*  &&  sh restore-offline.sh
set -e
echo "==> 检查 python（Termux 自带）"
command -v python >/dev/null || pkg install -y python
echo "==> 解包 site-packages（无需编译、无需网络）"
SITE=$(python -c 'import site; print(site.getsitepackages()[0])')
tar -xzf site-packages.tgz -C "$(dirname "$SITE")"
echo "==> 写入启动脚本与演示 workspace"
install -D agt-demo/AGENTS.md ~/agt-demo/AGENTS.md
install -m 0755 start-agt.sh ~/start-agt.sh
mkdir -p ~/.shortcuts && cp ~/start-agt.sh "$HOME/.shortcuts/Agt演示" && chmod +x "$HOME/.shortcuts/Agt演示"
echo ""
echo "🎉 完成：~/start-agt.sh 启动（或桌面 Termux:Widget「Agt演示」）"
echo "   首次：浏览器 http://127.0.0.1:8000 → 设置 → 添加模型"
EOF
# 小文件随包（start 脚本 + AGENTS.md）
cp ~/start-agt.sh "$PKG/start-agt.sh"
cp ~/agt-demo/AGENTS.md "$PKG/agt-demo-AGENTS.md" 2>/dev/null || mkdir -p "$PKG/agt-demo" && cp ~/agt-demo/AGENTS.md "$PKG/agt-demo/AGENTS.md"

# 归一成单文件 tgz 便于传输
echo "==> 汇总为单文件包"
tar -czf "$PKG.tgz" -C "$(dirname "$PKG")" "$(basename "$PKG")"
rm -rf "$PKG"
echo ""
echo "🎉 离线包：$PKG.tgz（$(du -h "$PKG.tgz" | cut -f1)）"
echo "   传给别人（微信/USB/网盘均可）→ 对方：装 Termux → 解压 → 跑 restore-offline.sh"
