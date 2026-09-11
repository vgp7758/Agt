# AgentID 身份工具 · agentid_tools（ModelScope Agent Identity Protocol）

> 个人身份工具（**不随包播种**）：`tools/builtin/agentid_tools.py`，改完 `/reload tools` 热加载。为 agt 提供正式魔搭（ModelScope）数字身份——Ed25519 密钥对签发短期 JWT，进任何接入 Agent Identity Protocol 的应用（DojoZero 比赛、PawFriends 社区等）。

## 身份与存储

- **AgentID**：`modelscope:agent_l75ig64zrcgz`（modelscope 命名空间 + 平台分配 agent id）；**kid**：`sdk-f89c9d35`
- **密钥对**：Ed25519，存 `~/.agt/.agentid/modelscope/agents/agt/`（**官方目录约定**，与 agent-id-client-sdk 一致）
- **依赖**：cryptography + requests（引擎环境均有）；不随包播种（个人身份工具）

## 签发协议（与官方 SDK 逐字节比对一致）

1. 私钥对 `agent_id|kid|audience|timestamp` 四段（`|` 连接）做 **Ed25519 签名**（base64url 无 padding 编码）
2. `POST {idp}/agent_id/token` 换目标应用（audience）的**短期 JWT**

> 协议并非文档得来——报错驱动试探 4 轮（必填五元组 → base64url 格式 → 签名内容 = 四段拼接）锁定，后装官方 `agent-id-client-sdk` 读源码比对**逐字节一致**（2026-09-11 实测破解）。

- 链路探活实证：伪造 audience 签发 → IDP 返回 404 `ResourceNotFound`——**签名验证通过**（走到了业务层才报"应用不存在"）

## 两个工具

| 工具 | 用途 |
|---|---|
| `agentid_status` | 查看本地身份状态（agent_id / 密钥就位 / 缓存 token） |
| `agentid_get_token(audience)` | 为目标应用签发短期 JWT；`_TOKEN_CACHE`（{audience, ...}）缓存**自动续签**，不用每次手签 |

## DojoZero SDK 接线（2026-09-11）

- `dojozero-client` 已装；公共服务器 `api.dojozero.live` 已配置；`discover` 探活连通
- **卡点①**：audience（DojoZero gateway 的 hub client_id）文档明确"from the contest operator"——公开端点不暴露；获取途径 = dojozero.live 接入页 / 官方钉钉群（44837352）
- **卡点②**：当前无开放比赛（`discover` 返回 `No trials available`）
- 基础设施就绪：拿到 hub client_id + 新比赛开放后，`agentid_get_token(audience)` → `dojozero-agent start` 进场，链路全现成

## 注册形态

`agt_register()` **无参**（身份读 `~/.agt/.agentid/`，不依赖 workspace，无需 ctx）；两工具 `hidden: False` 进正常工具集；`/reload tools` 实测注册 **52 个（50 旧 + 2 新）**。

## 与其他模块的关系

- 属 [工具外置](tool-externalization.md) 体系的用户个人外置件，**不随包播种**（与 [zai-tools](zai-tools.md) 同类，区别于 cache_tools / explore_tools 等随包副本）
- 身份目录 `~/.agt/.agentid/` 走 AGT_HOME 路径族语义（kv_tools / zai_tools 同款）

## 注意事项

- audience 是目标应用在其 IDP 注册的 client_id——**不是公开常量**，需从各应用侧获取（DojoZero 找 contest operator）
- 短期 JWT 由 `_TOKEN_CACHE` 缓存续签，到期前自动重签
- 密钥丢失不可恢复（Ed25519 私钥即身份本体），`~/.agt/.agentid/` 属敏感目录

## 相关页面

- [工具外置](tool-externalization.md) —— 外置件清单与装配
- [zai-tools](zai-tools.md) —— 同目录用户个人工具另一例（智谱联网三件套，不随包）