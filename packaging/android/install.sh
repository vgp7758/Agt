#!/data/data/com.termux/files/usr/bin/sh
# agt-agent Android(Termux) 一键安装 —— https://github.com/vgp7758/Agt
# 用法：把本文件放到 Termux 家目录，执行  sh install.sh
# 前置：已安装 Termux（F-Droid 或 GitHub Releases 下载，应用商店没有）
set -e
STEP() { echo ""; echo "======== $* ========"; }

STEP "0/5 环境自检"
if [ ! -d /data/data/com.termux ]; then
  echo "❌ 请在 Termux 里运行本脚本（不是普通 Linux shell）"; exit 1
fi
echo "Termux OK：$(uname -m)"

STEP "1/5 更新系统包源"
pkg update -y || true

STEP "2/5 安装 python + 编译工具链（rust：pydantic-core 需本地编译，几分钟属正常）"
pkg install -y python rust binutils libffi openssl

STEP "3/5 pip 国内镜像安装 agt-agent"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
python -m pip install -U pip -i "$PIP_MIRROR" >/dev/null 2>&1 || true
export CARGO_NET_GIT_FETCH_WITH_CLI=true
if ! pip install agt-agent -i "$PIP_MIRROR"; then
  echo ""
  echo "❌ 安装失败。最常见原因：pydantic-core 编译缺 rust——确认第 2 步 rust 装成功后重跑本脚本"
  exit 1
fi
echo "已安装：$(pip show agt-agent | grep -i ^Version)"

STEP "4/5 初始化演示 workspace + 启动脚本"
mkdir -p ~/agt-demo
cat > ~/agt-demo/AGENTS.md <<'EOF'
# Agt 手机演示环境

## 你是谁/在做什么
你是一个运行在【Android 手机 Termux 环境】中的 Agt Agent——纯手机本地跑的 AI Agent，
通过手机流量调用云端 LLM，工具（文件读写/代码执行等）都在手机本地执行。

## 展示要点（用户让你"介绍运行环境"时）
- 说明自己跑在手机里（Termux + Python），不是云端服务器
- workspace 就是手机上 ~/agt-demo 目录，可以让用户让你读写文件演示
- 会话/记忆/工作流与电脑版同构（~/.agt 数据目录）
EOF
cat > ~/start-agt.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
# 一键启动：起服务 + 自动打开浏览器
pkill -f "python.*agt" 2>/dev/null || true
cd ~/agt-demo
nohup agt-web 8000 > ~/agt-demo/agt-web.log 2>&1 &
echo "服务启动中…（日志 ~/agt-demo/agt-web.log）"
sleep 5
termux-open-url "http://127.0.0.1:8000" 2>/dev/null || \
  echo "请手动打开浏览器访问 http://127.0.0.1:8000"
EOF
chmod +x ~/start-agt.sh
# Termux:Widget 桌面一键启动（需另装 Termux:Widget 应用）
mkdir -p ~/.shortcuts
cp ~/start-agt.sh "$HOME/.shortcuts/Agt演示"
chmod +x "$HOME/.shortcuts/Agt演示"

STEP "5/5 安装完成 🎉"
cat <<'EOF'

启动方式（任选）：
  ① 命令行     ~/start-agt.sh
  ② 桌面图标   安装 Termux:Widget（F-Droid）→ 手机桌面添加小部件 → 选「Agt演示」

首次使用（1 分钟）：
  浏览器打开 http://127.0.0.1:8000 → 右上角设置 → 模型 → preset 里选一个
  → 填 api_token 保存（走手机流量）

演示开场白建议：
  「介绍一下你现在的运行环境」——它会告诉你自己跑在手机 Termux 里
EOF
