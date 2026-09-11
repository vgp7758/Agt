# SCNet 算力网 · 外部 GPU 平台接入与侦察记录（www.scnet.cn）

## 定位

SCNet（四川/九章算力网？控制台 www.scnet.cn）是外部 GPU 算力平台，按「卡时」售卖异构加速卡与 NVIDIA 卡。本页记录 agt 接入该平台的通道、镜像机制与实探资源，供后续做分布式/K8s 实验时查阅。

> 当前所有信息来自 playwright 浏览器侦察（2026-09），登录态已通过扫码建好，后续可复用。

## 权限通道（三条）

| 通道 | 用途 | 状态 |
|---|---|---|
| **API Key** | 调平台上的 LLM 服务，可加成 agt 的 provider | 待用户创建 key |
| **Playwright 浏览器** | 控制台全操作（开 Notebook/容器、选卡、传文件、看账单） | ✅ 已生效 |
| **E-Shell 网页命令行** | 实例起来后直接敲命令；另有「文件管理」 | 就绪 |

- 算力编排（开卡/关机）**无公开 API**，playwright 是唯一自动化路径。
- 控制台「密钥管理」页配额 50 个 key。
- 权限的实用语义：用户在 playwright 窗口扫码登录 = 授权完成（登录态即操作权），无需额外配置。

## LLM API 端点与 provider 接线

- OpenAI 兼容：`https://api.scnet.cn/api/llm/v1`
- Anthropic 兼容：`https://api.scnet.cn/api/llm/v1/anthropic`
- 接入 agt：拿到 key 后按 `guides/config-and-models.md` 的 provider 档案手工加一条即可；可白嫖其 GLM/DeepSeek 服务。

## 自定义镜像：commit 式（非 Dockerfile/制品仓库）

官方文档《镜像说明》的机制不是构建/制品仓库，而是 **commit 式**：

```
基础镜像开 Notebook → 装环境 → 更多操作→「保存镜像」→ 存入我的镜像
→ 下次开实例直接选它（单层 ≤ 15GB）
```

- Notebook 关机**秒级保存开发环境**（新功能），小改甚至不用存镜像。
- 镜像选择另有「社区镜像」tab（stable-diffusion-webui、yolov5 等 AIGC 现成镜像）。
- 「我的镜像」独立页面是需付费开通的增值服务；Notebook 自带的保存镜像无需它。

## 资源实况（控制台实测单位：单卡·时，2026-09）

| 卡 | 显存 | 区域 | 可用/总 | 价格 | 备注 |
|---|---|---|---|---|---|
| 异构加速卡AI | 64GB | 昆山 021 | 4/4 | ¥2.53 | 推荐组，1000+ 卡池 |
| 异构加速卡AI | 64GB | 昆山 015 | 1/8 | ¥3.6 | 内测 |
| 异构加速卡AI | 64GB | 四川 097 | 8/8 | ¥3.6 | |
| 异构加速卡1 | 16GB | 昆山 079 | 3/4 | ¥2 | （用户记的 ¥2 是这张，非 L20） |
| **NVIDIA L20** | **48GB** | 昆山 012 | 0/8 | ¥2.53/3.6 | 标「限」，CUDA≤12.4 |
| NVIDIA A800 | 80GB | 山东 016 | 2/8 | ¥6.9 | CUDA 12.4 |
| NVIDIA A800 | 80GB | 广东 064 | 0/4 | ¥7.8 | NVLink，CUDA 13.0 |
| 4090 / 容器组(武汉) | 24/16GB | — | — | ¥6.9 / ¥0 | 容器组 K8s 138 组免费 |

- 容器服务文案：「全新升级为基于 K8S 调度的容器组」（武汉 k8s 集群1）——配合 k8s 完成任务的设想被证实。

## 侦察留痕（playwright 产物）

本轮侦察由 playwright MCP 完成，产物落在 workspace 根：

| 文件 | 内容 |
|---|---|
| `scnet_qr.png` | 商城/控制台登录二维码截图（**含登录凭据，勿提交仓库**） |
| `qr_dataurl.txt` | 二维码 dataURL（`document.querySelectorAll('img')` 中 `naturalWidth>=200` 的 base64 PNG） |
| `scnet_console.png` | 控制台 dashboard 截图（`/ui/console/index.html#/space/dashboard`） |

- 登录方式：用户在 playwright 窗口扫码 → 登录态留存在该浏览器上下文，后续轮次可直接操作控制台。
- 侦察路径：`https://www.scnet.cn/ui/mall/core`（核心节点商城）→ 控制台 dashboard。

## 待办与注意事项

- **实名认证【未认证】**：左侧栏显示未认证，大概率影响购买卡时；需先到 `个人中心 > 实名认证`。
- **50 卡时免费额度**：费用总览余额区为 `****`，无单独券显示；可能在商城商品页领取或下单时自动抵扣，待实名+创建 key 后验证。
- 下一步候选：给 API Key 加 provider，或开免费容器组试 K8s。

## 相关页面

- [配置体系与模型调优](config-and-models.md)
- [本地模型](local-models.md)
- [运维、可观测性与排障](ops.md)