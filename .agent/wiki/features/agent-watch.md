# agent_watch · 本地实例自动发现 + 状态监视 + 变化邮件通知（tools/agent_watch.py，2026-09-20·用户提案）

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
- 配置全部在脚本顶部 CONFIG 区：`INTERVAL` / `STATE_FILE` / `MAIL`（QQ SMTP 授权码）/ `STATIC_META`（本地端口 → 别名/备注，**仅注释用途**）/ `REMOTE_WATCHES`（远程静态实例）——旧 `WATCHES` 静态监视清单已删除，本地监视对象由自动发现（见下）每轮生成
- 部署形态（2026-09-20 · 二 起）：**独立进程**常驻（DETACHED 启动）——不挂在 9000 的服务树里，9000 `/restart` 不影响监视（详见[部署清单](#部署清单)）

## 数据流

```
agent_watch.py（本机常驻 · 独立进程，不在任何实例的服务树里）
  ├─ build_watches()：每轮重建监视清单
  │    ├─ 本地 = local_agt_ports() 自动发现：netstat 扫 LISTENING 端口
  │    │    → 逐个 POST /api/status 验证（响应含 session_name 即 agt 实例）
  │    │    → STATIC_META 仅补别名/备注，未登记端口命名 port-<N>
  │    ├─ 公网域名 = cpolar_domain()：扫 ~/.cpolar/logs/cpolar_service.log*（mtime 新→旧）
  │    │    取最新 *.cpolar.top —— 每轮重扫，免费版重连换域名也能跟上
  │    └─ 远程 = REMOTE_WATCHES 静态（cnb.run 等无法自动发现）
  └─ 每 900s → POST {各实例}/api/status（timeout 8s；配 token 的实例 401 时带 X-Cb-Token 重试）
       → 指纹 (alive, session, turns, busy, inbox)
       → 与状态文件上一轮对比
       → 有变化 → SMTP_SSL 465 发 QQ 邮箱（每实例附「局域网 + 公网 /port-N」地址块）；无变化 → 整轮跳过
```

## 本地实例自动发现：netstat 扫描 → /api/status 验证（2026-09-20 · 二，用户问出）

起因：用户问「**是本地在跑的实例都自动监听了，还是只监听了其中这几个？**」——原版答案尴尬：静态 `WATCHES` 只手工列了 4 个，新实例起在别的端口要手加、关了还占着清单。本轮改为全自动发现。

### 机制（`local_agt_ports()` + `build_watches()`）

1. `netstat -ano` 收集本机全部 LISTENING 端口（timeout 15s）
2. 排除已知非 agt 端口（4040/6060/9200 = cpolar 管理口、7897 = 代理）
3. 逐个 `POST http://127.0.0.1:{p}/api/status`（timeout 3s）——响应含 `session_name` 即认定为 agt-web 实例
4. `build_watches()` **每轮重建**清单：`STATIC_META`（9000/8000/50051）降级为**别名/备注表**不再驱动监视，未登记端口自动命名 `port-<N>`

推论：**实例下线期间自然退出监视、恢复监听自动回归**（claw-50051 本轮即如此）；剧组开机/关机、任意端口起新实例，最迟 15 分钟后自动进出监视与邮件。

### 首轮实测（2026-09-20）：发现 7 个本地实例

main-9000 自我迭代（1028 轮）、agt-8000 多媒体专家（779）、**port-8100 导演（70）/ port-8101 编剧（8）/ port-8102 脚本师（9）/ port-8103 程序（49）——剧组四件套**、port-9300 SCNet算力运维（73）。后 5 个在静态清单时代**根本不存在于监视里**——自动发现的价值当场兑现。

## cpolar 公网地址自动读取 + /port-N 路由（2026-09-20 · 二，用户两条提示）

用户两条提示全部落地：

### 域名从日志读（`cpolar_domain()`）

`~/.cpolar/logs/cpolar_service.log*` glob → 按 mtime **新→旧**逐个读 → 正则抽 `*.cpolar.top|.cn` → 取日志里**最后出现**（≈最新一次隧道建立）。当前读到 `4150f500.r22.cpolar.top`；**每轮重扫**——免费版重连换域名也能自动跟上。

- `_safe_mtime` 竞态防护：cpolar 日志滚动（rotation）瞬间 `getmtime` 会抛 OSError——包 try/except 返 `0.0`，读文件再套 `errors="replace"` + 逐文件 try，任何单文件异常不阻断发现流程

### `/port-N` 路由：一条隧道通本机任意端口

`build_watches()` 给每个自动发现的本地实例自动拼 `public = https://{域名}/port-{p}`。实测：`/port-9000` → 自我迭代（1028 轮）✓、`/port-8000` → 多媒体专家（779 轮）✓。邮件地址块每实例自动带两行：

```
- 局域网：http://192.168.228.233:8100
- 公网：https://4150f500.r22.cpolar.top/port-8100
```

### 收尾清理

cpolar.yml 里此前为 9000 单独加的 agt 隧道**已删除**——一条隧道 + `/port-N` 就够，逐实例建隧道是多余的；配置恢复原样，**用户无需重启 cpolar 服务**（上一版「已写配置、待重启生效」的待办就此作废）。

### ⚠️ 域名会漂移

免费版重连即换域名（实测已从 scnet 回调时代的 `75a28242.r21` 漂到 `4150f500.r22`）——agent_watch 每轮重扫自愈；其它**硬编码该域名**的消费端（如容器侧 monitor 的回调 URL）不会自动跟进，漂移后需手动更新。

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

## 部署清单（2026-09-20 · 二 实况）

**部署形态（2026-09-20 · 二）**：独立进程常驻（pid 19452，DETACHED 启动）——**不在 9000 的服务树里**，9000 `/restart` 不影响监视；代码已 commit（`feat(tools): agent_watch 自动发现 + cpolar /port-N 公网路由`，push 随下次提交带出；初版 pid 13852 已被本版取代）。

监视对象（本地 7 个 = 自动发现，远程 1 个 = 静态；本地无需任何配置）：

| 实例（watch 名） | session | 局域网 | 公网 | 备注 |
|------|--------|------|------|------|
| main-9000 | 自我迭代（1028 轮） | `192.168.228.233:9000` | `…/port-9000` ✓ 实测 | 主 Agent；STATIC_META 别名 |
| agt-8000 | 多媒体专家（779） | `192.168.228.233:8000` | `…/port-8000` ✓ 实测 | 实测闭环样本；别名 |
| port-8100~8103 | 导演 70 / 编剧 8 / 脚本师 9 / 程序 49 | `:8100-8103` | `…/port-810x` | **剧组四件套——自动发现新收编**（静态清单时代不在监视里） |
| port-9300 | SCNet算力运维（73） | `:9300` | `…/port-9300` | agt_scnet |
| claw-50051 | agt-worker | `:50051` | `…/port-50051` | 本轮发现时不可达、后恢复在线（下一轮报 🟢）；别名 |
| brick（远程静态） | — | — | `https://iqhxsci1es-8000.cnb.run` | `/api/status` 转发层曾吃 401（带 X-Cb-Token 也 401）；**2026-09-21 首轮回收换 `nai0dl67kj` → 巡检首次实战再换 `iqhxsci1es`，REMOTE_WATCHES 均已同步**——401 实为容器死亡的表现（回收即转发失效），新短链恢复后待邮件验证；回收/复活全程见 [OKX A2A](okx-a2a.md) |

表中 `…/port-N` 即 `https://<当前 cpolar 域名>/port-N`（域名每轮从日志重扫，当前 `4150f500.r22.cpolar.top`，见上节）。

> 2026-09-21 补记：CNB 双容器被回收 → A 容器复活换新短链。brick 属**远程静态清单**（cnb.run 无法自动发现），地址要人工/巡检同步——本机保活巡检 cnb-brick-keepalive（[OKX A2A · 保活根治](okx-a2a.md)）的不通分支已把「更新 REMOTE_WATCHES」纳入标准动作。
>
> 2026-09-22 补记（保活巡检首次实战 → 复用验证）：03:53 巡检发现 `nai0dl67kj` → 401 + SSH 拒 → 判定容器再次被 CNB 回收 → 自动复活（新容器 + 新短链 `iqhxsci1es-8000.cnb.run`）→ **REMOTE_WATCHES 已同步新短链（本轮即 tools/agent_watch.py 的变更内容）**。注意远端重建伴随钱包登录态跨容器失效（见 [OKX A2A · 保活巡检首次实战](okx-a2a.md#保活巡检首次实战cnb-再回收--自动复活闭环2026-09-220353)），agent_watch 只能反映「alive/session/turns」变化，登录态需巡检的 wallet 检查兜底。

## 实测闭环（2026-09-20）

`remote_message` 给 8000 发验证消息 → 等它回复 → `agent_watch.py --once` → 检出「💬 新增 1 轮回答（778 → 779）」→ 邮件已发到 foxmail ✓（主题「【Agent Watch】1 项变化」）。

## 关键实现

- `local_agt_ports()` / `build_watches()`：自动发现与清单重建（见上「本地实例自动发现」节）
- `cpolar_domain()`：cpolar 日志提取公网域名（见上「cpolar 公网地址自动读取」节）
- `lan_ip()`：UDP connect `8.8.8.8:80` 取本机局域网 IP（不发包）
- `probe(w)`：POST `/api/status`；实例配 `token` 时先无 token 试一次，401 再带 `X-Cb-Token` 重试
- `run_once()`：一轮完整执行，返回是否发信；`main()` 常驻 while 循环，单轮异常不杀死进程
- 地址块：`w["url"]` 里的 `127.0.0.1` 替换为 `lan_ip()` 得局域网地址（开发轮内修复：废弃了错误的端口提取代码，改为直接替换，commit 内随建随修）；公网行取 `w["public"]`——自动发现实例 = cpolar `/port-N` 路由，远程实例用静态 url
- 指纹 = `(alive, session, turns, busy, inbox)` **不含 model**——模型切换不触发邮件，但邮件「状态：」行会带当前模型

## 注意事项

- **SMTP 授权码明文**在 CONFIG 区——本机私有工具可接受，勿外发脚本
- 邮件发送失败只打印 stderr、不中断循环（`send_mail` 返回 bool）
- agent_watch 是**纯读轮询**，与 [外部事件注入](external-injection.md)（脚本 → 实例）方向相反；如需实例主动推状态可结合 `/api/callback` 通道
- 改别名/备注（`STATIC_META`）、远程清单（`REMOTE_WATCHES`）、邮箱/间隔都在 CONFIG 区，改完用 `--once` 立即验证；**本地实例零配置**（自动发现，无需登记）
- 预期行为：部署后 15 分钟那一轮会先发一封（对话本身即新 answer）；之后无互动则不再有邮件
- `local_agt_ports()` 的非 agt 端口排除表（4040/6060/9200/7897）写死在代码里；误判防线是 `/api/status` 响应须含 `session_name`——非 agt 服务即便撞上扫描也只会被跳过
- cpolar 免费版域名随重连漂移——agent_watch 每轮重扫自愈，其它硬编码该域名的消费端不会（见「cpolar 公网地址自动读取」节）

## 相关页面

- [ /api/status 端点](api-status.md) — 指纹数据来源
- [多实例组网](../architecture/multi-instance.md) — 监视对象与地址体系
- [外部事件注入](external-injection.md) — 反向通道（脚本 → 实例）
- [运维与排障](../guides/ops.md) — 常驻进程、可观测性