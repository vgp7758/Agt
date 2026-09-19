# agent_watch · 本地 Agent 实例状态监视 + 变化邮件通知（tools/agent_watch.py，2026-09-20·用户提案）

> 源码：`tools/agent_watch.py`（本机常驻循环，2026-09-20 已部署 pid 13852）。
> 每 15 分钟轮询各实例 [ `/api/status`](api-status.md)，指纹有变化才发邮件（附局域网 + 公网地址）；多实例静默期零打扰、首轮只建基线不发信。

## 职责

把「各 agt 实例最近有没有动静」主动推给用户邮箱。解决多实例部署的**被动关注问题**——实例在后台产出 answer、切 session、忙闲切换，用户不主动打开 WebUI 就无从得知。配合 [多实例组网](../architecture/multi-instance.md) 的地址体系，邮件里直接给出每个实例的**局域网 + 公网入口**。

## 用法

```bash
python agent_watch.py            # 常驻循环（默认 900s）
python agent_watch.py --once     # 跑一轮退出（测试手发）
python agent_watch.py --baseline # 强制重建基线（不发信）
```

- 状态文件：`~/.agt/agent_watch_state.json`（上一轮指纹；文件缺失 = 首轮，只建基线不发信）
- 配置全部在脚本顶部 CONFIG 区：`INTERVAL` / `STATE_FILE` / `MAIL`（QQ SMTP 授权码）/ `WATCHES`（监视清单）

## 数据流

```
agent_watch.py（本机常驻）
  └─ 每 900s → POST {各实例}/api/status（timeout 8s；配 token 的实例 401 时带 X-Cb-Token 重试）
       → 指纹 (alive, session, turns, busy, inbox)
       → 与状态文件上一轮对比
       → 有变化 → SMTP_SSL 465 发 QQ 邮箱；无变化 → 整轮跳过
```

## 指纹与变化事件

指纹 = `(alive, session, turns, busy, inbox)`（`fingerprint()`）。`diff_events()` 产出人类可读事件：

| 事件 | 触发 |
|------|------|
| 🟢 上线 | 此前不可达 → 可达 |
| 🔴 不可达（附错误） | 可达 → 不可达；**持续离线不重复报** |
| 💬 新增 N 轮回答 | turns 增加（主事件——实例产出新 answer） |
| 🔁 会话切换 / 轮数回退 | session 名变 / turns 减少（重开会话/回溯） |
| ⏳ 开始忙碌 / ✅ 空闲 | busy 翻转 |
| 📥 inbox +N | 排队消息增加 |

邮件纪律（用户提案的核心语义）：
- **无变化的实例不出现在邮件里**；全员无变化 → 整封跳过
- 首轮（无状态文件）只建基线不发信；`--baseline` 可强制重建

## 部署清单（2026-09-20 实况）

| 实例 | 局域网 | 公网 | 备注 |
|------|--------|------|------|
| main-9000（主 Agent） | `192.168.228.233:9000`（lan_ip 探测） | cpolar agt 隧道**已写配置、待重启生效** | 生效后把真实域名填进 `public` 字段 |
| agt-8000（多媒体专家） | `192.168.228.233:8000` | 无 | 实测闭环用样本 |
| claw-50051（ClawTasks 备用） | `192.168.228.233:50051` | 无 | **当前不可达**（连接被拒，未擅自拉起——备用实例待用户裁定） |
| brick（CNB 云容器 #13789） | — | `https://adk2zs60ym-8000.cnb.run` | `/api/status` 返回 401（带 X-Cb-Token 也 401——cnb.run 转发层鉴权问题）；**不影响页面浏览与聊天**；容器重建换新短链后需同步更新 WATCHES |

## 实测闭环（2026-09-20）

`remote_message` 给 8000 发验证消息 → 等它回复 → `agent_watch.py --once` → 检出「💬 新增 1 轮回答（778 → 779）」→ 邮件已发到 foxmail ✓（主题「【Agent Watch】1 项变化」）。

## 关键实现

- `lan_ip()`：UDP connect `8.8.8.8:80` 取本机局域网 IP（不发包）
- `probe(w)`：POST `/api/status`；实例配 `token` 时先无 token 试一次，401 再带 `X-Cb-Token` 重试
- `run_once()`：一轮完整执行，返回是否发信；`main()` 常驻 while 循环，单轮异常不杀死进程
- 地址块：`w["url"]` 里的 `127.0.0.1` 替换为 `lan_ip()` 得局域网地址（开发轮内修复：废弃了错误的端口提取代码，改为直接替换，commit 内随建随修）

## 注意事项

- **SMTP 授权码明文**在 CONFIG 区——本机私有工具可接受，勿外发脚本
- 邮件发送失败只打印 stderr、不中断循环（`send_mail` 返回 bool）
- agent_watch 是**纯读轮询**，与 [外部事件注入](external-injection.md)（脚本 → 实例）方向相反；如需实例主动推状态可结合 `/api/callback` 通道
- 改监视清单/邮箱/间隔都在 CONFIG 区，改完用 `--once` 立即验证
- 预期行为：部署后 15 分钟那一轮会先发一封（对话本身即新 answer）；之后无互动则不再有邮件
- cpolar 公网隧道：9000 的配置已写入，但重启 cpolar 服务需管理员权限（`net stop cpolar && net start cpolar`）

## 相关页面

- [ /api/status 端点](api-status.md) — 指纹数据来源
- [多实例组网](../architecture/multi-instance.md) — 监视对象与地址体系
- [外部事件注入](external-injection.md) — 反向通道（脚本 → 实例）
- [运维与排障](../guides/ops.md) — 常驻进程、可观测性