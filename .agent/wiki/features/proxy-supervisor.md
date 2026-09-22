# proxy_supervisor · 本地 LLM 聚合代理（repo 外基础设施，D:\Programs\env\）

> ⚠️ 本页记录的是 **Agt 仓库之外**的本机基础设施脚本（`D:\Programs\env\proxy_supervisor.py` + `proxy_cc.py`，不受 agt repo git 管理）。wiki 收录原因：它是 models.json `proxy` provider 背后的服务端，与 [上下文引擎](../architecture/context-engine.md) 的投影体积直接耦合——投影越大请求体越大，曾撞上 aiohttp 默认 1MB 字节墙（见下）。

## 职责

- 本机常驻代理服务：Agt 各实例的 `proxy` provider 端点都打到它，由它转发/聚合到真实上游（/stats 的 `resp_model` 可见其内部路由，见 [ops · /stats](../guides/ops.md#stats-页webui-统计按钮)）
- 双层 aiohttp 应用：**supervisor 根 app**（sup_app）+ 经 `add_subapp` 挂载的 **proxy_app**（`/api/llm` 等 LLM 请求入口在 subapp 上）
- 相关配置背景：`proxy` 模型卡片的缓存经济学参数（曾因未配 `detail_step` 回落全局值引发轮内小毕业，见 [cache-tools · 诊断链](../features/cache-tools.md#元组解包修复_identify_zone-tool-分支漏第二项2026-09-15commit-3cf55e9用户实测-t877_s51-抓到)）

## 413 字节墙：aiohttp 默认 1MB 请求体上限（2026-09-22 修复）

### 现象

大投影轮的 LLM 调用恒 413：`Maximum request body size 1048576 exceeded...`——35 万+ 字符的投影发不过去（1048576 = 1MB，正是 aiohttp 默认值的铁证）。

### 根因三层

1. **框架默认值，不是谁写的逻辑**：`web.Application()` 不传 `client_max_size` 时默认 `1024**2`（1MB）；handler 里 `await request.read()` 读 body 时逐块检查，超限抛 413。
2. **subapp 的设置形同虚设（关键陷阱）**：用户其实早就防过这一手——`proxy_cc.py:2074` 写了 `client_max_size=256MB`（注释还特意提了 aiohttp 默认值会 413 大请求）。但它设在 `proxy_app` 上，而 `/api/llm` 是 `sup_app.add_subapp()` 挂进来的。核对本机 aiohttp 3.12.15 源码：**request 对象由根 app 的 `_make_request` 工厂创建，`client_max_size` 在构造 Request 时就固化成根 app 的值，subapp 的设置从来不生效**。
3. **上游没有这道墙**：历史实测 t405（2026-08）投影曾到 **187 万字符**（几 MB 字节）照常过智谱直连——1MB 这道墙只存在于本地 proxy 这一层。

### 修复（一行）

```python
# D:\Programs\env\proxy_supervisor.py 第 611 行（根 app 构造处）
- app = web.Application()
+ app = web.Application(client_max_size=256 * 1024 * 1024)
```

- 原文件备份：同目录 `proxy_supervisor.py.bak_20260922_bodylimit`
- 改后重启进程（旧 5340 → 新 8004），现由主 Agent 后台服务 `proxy-supervisor-9877` 代管
- **与 subapp 里已有的 256MB 值对齐**——补的是根 app 这一层

### 与投影/压缩无关（300k~400k tok 区间立场兼容）

修的是**传输层允许多大 body 进来**，不是让投影变小。session 侧零改动：折叠粘性、分档、`fold_target`、窗口参数全部原样——不到 40 万 tok 顶窗不动，真顶窗才按保留线压回（机制见 [context-engine](../architecture/context-engine.md)）。用户口径 300k~400k tok 稳定区间下：顶到 win=40 万 tok ≈ 1.2MB 字节，离 256MB 上限差 **200 倍**，永远撞不到墙。

### 验证

| 调用 | 通道 | 投影字符 | 结果 |
|---|---|---:|---|
| t1096 s17/s18（修复后） | glm-official 直连 | 378,466 | ✅ |
| t1097 s0/s1（修复后） | proxy | 380,496 | ✅ |

session 已恢复工作，35.5 万 tok 级投影原样进出。

## 注意事项

- **换网关要看 body 上限**：aiohttp（默认 1MB）、nginx（默认 `client_max_body_size 1m`）等框架/中间件都有各自的默认墙——投影体积 1MB+ 是这个系统的常态，部署代理链时逐层核对。
- **遗留待办（防御性，不急，等用户拍板）**：「字节级保命阀」spec——防未来换别的网关又冒出暗墙；本次修复未包含。
- 排障入口见 [ops · 常见错误对照](../guides/ops.md#常见错误对照) 的 413 行。

## 相关页面

- [上下文引擎与缓存优化](../architecture/context-engine.md) — 投影装配、分档折叠、窗口/fold_target（本次修复刻意不动的那一侧）
- [配置体系与模型调优](../guides/config-and-models.md) — models.json provider 档案，`proxy` 条目指向本服务
- [cache-tools](../features/cache-tools.md) — proxy 模型卡片步距衰减曾致轮内小毕业的诊断链
- [运维、可观测性与排障](../guides/ops.md) — llm_calls.jsonl / /stats 排障入口
