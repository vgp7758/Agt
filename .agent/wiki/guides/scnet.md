# SCNet 算力网 · 外部 GPU 平台接入与侦察记录（www.scnet.cn）

## 定位

SCNet（四川/九章算力网？控制台 www.scnet.cn）是外部 GPU 算力平台，按「卡时」售卖异构加速卡与 NVIDIA 卡。本页记录 agt 接入该平台的通道、镜像机制与实探资源，供后续做分布式/K8s 实验时查阅。

> 当前所有信息来自 playwright 浏览器侦察（2026-09），登录态已通过扫码建好，后续可复用。

## 权限通道（三条）

| 通道 | 用途 | 状态 |
|---|---|---|
| **API Key** | 调平台上的 LLM 服务，可加成 agt 的 provider | 待用户创建 key |
| **AK/SK 直连 API** | ① 官方 OpenAPI（HPC 作业/文件/集群）+ ② **containermgt Notebook 后端**（纯 HTTP，见下节） | ✅ **已实测通（区域 token 即钥匙）** |
| **Playwright 浏览器** | 控制台全操作（开 Notebook/容器、选卡、传文件、看账单）；写操作 payload 抓包取证 | ✅ 已生效（**已降级为兜底**） |
| **E-Shell 网页命令行** | 实例起来后直接敲命令；另有「文件管理」 | 就绪 |

- 算力编排（开卡/关机）**已有纯 API 路径**（containermgt，只读已验证、写操作待补 payload）；playwright 保留为兜底与抓包通道。
- 控制台「密钥管理」页配额 50 个 key。
- 权限的实用语义：用户在 playwright 窗口扫码登录 = 授权完成（登录态即操作权），无需额外配置；而纯 API 只需 `~/.agt/scnet.json` 里的 AK/SK。

## 纯 API 通道实测：containermgt 与控制台共用同一 JWT（2026-09-14）

**结论：playwright 不再是唯一自动化路径——Notebook 全生命周期有纯 HTTP 后端，且鉴权钥匙就是已有的 AK/SK 区域 token。**

## 三层 API 地图

| 层 | 端点 | 鉴权 | 覆盖 | 实测 |
|---|---|---|---|---|
| ① 官方 OpenAPI | `api.scnet.cn` / `zzhpc.scnet.cn:65051` | AK/SK HMAC 签名 → 换区域 token | HPC 作业、文件、集群 | ✅ 已通（`scnet_mcp`） |
| ② **控制台容器管理后端** | **`cancon.hpccube.com:65011/acx/containermgt/v2/notebook/*`** | **同一把区域 token（`token` header）** | **Notebook 全生命周期** | ✅ **只读已实测** |
| ③ 容器内 API | Jupyter Contents/Terminal、ComfyUI :8190 | Jupyter token / 无 | 文件、终端、任务 | ✅ 已在用 |

## 关键发现：② 与 ① 共用同一 JWT

浏览器里 containermgt 请求的 `token` header 解出来是 `{computeUser: scnrilsyy5, clusterId: 11250, user: brick}`——与 AK/SK 换来的**昆山区域 token 完全一致**（创建实例后该区域 token 自动出现在 token 列表）。

**AK/SK 换 token 实测返回**（`POST https://api.scnet.cn/api/user/v3/tokens`，HMAC 签名见上节「凭证用户名变更」）：

| clusterId | 区域 | computeUser | user |
|---|---|---|---|
| 11250 | 华东一区【昆山】 | scnrilsyy5 | brick |
| 20091 | 华中一区【A区】 | act3fh878f | brick |
| 0 | ac（platform） | root | brick |

## 实测端点（纯 urllib + token header，零 cookie）

```
GET /acx/containermgt/v2/notebook/list?clusterId=11250                    → 200 ✅ 实例列表全字段
GET /acx/containermgt/v2/notebook/{id}/notebook-url?clusterId=11250       → 200 ✅ Jupyter URL
```

实例对象信息量大：`notebookStatus`、`sshPassword`、`containerId`、`node`、`resourceGroupId:15`、`command` + `customsizePort:8191`（自定义服务配置）、`noCardMode:"1"` / `noCardCpuNum:"0.5核心"` / `noCardRamSize:"4GB"`——**「无卡模式」= 0.5 核 / 4GB 免费 CPU 实例**，API 参数齐全。

## 尚缺与价值

- **写操作未拿到**：创建实例 / 启停 / 配置自定义服务 / 关机的 payload 结构待补——`list` 返回字段是创建请求的反推源，再抓一次浏览器真实写请求即可补全（服务端参数校验会拦，不会误创建）。已定位候选端点：`/acx/aimgt/notebook`、`/acx/containermgt/notebook/task/actions/*`（前端 JS 提取，见「端点清单来源」）。
- **打开全无人值守**：`AK/SK 换 token → POST 创建 Notebook（选镜像/资源组/无卡或有卡）→ 轮询 notebookStatus 至 Running → POST 配自定义服务（端口+启动指令）→ 等 ComfyUI /system_stats → enqueue 批量 → monitor 回调（已通）→ POST 关机`。
- playwright 由此降级为「兜底 + 抓包取证」工具；现有巡检任务（`scnet_free_card_watch` / `scnet_img_sync`）可择机改写为纯 API。
- **只读段已完成**：list / notebook-url / config / start-command / port-pool 已封装进 MCP 工具 `scnet_notebook`（零浏览器）；**写操作段是当前唯一缺口**。

## LLM API 端点与 provider 接线

- OpenAI 兼容：`https://api.scnet.cn/api/llm/v1`
- Anthropic 兼容：`https://api.scnet.cn/api/llm/v1/anthropic`
- 接入 agt：拿到 key 后按 `guides/config-and-models.md` 的 provider 档案手工加一条即可；可白嫖其 GLM/DeepSeek 服务。

## 凭证用户名变更（act3fh878f → brick，2026-09-14）

用户改了 SCNet 平台登录用户名后，所有 scnet_* 工具报 `10009 用户不存在`。用直连 token 接口做三态最小复现对照，钉死了口径：

| 请求头 `user` | 签名消息里的 user | 结果 |
|---|---|---|
| `act3fh878f`（旧值） | act3fh878f | ❌ `10009 用户不存在` |
| `brick`（新值） | act3fh878f（旧签名） | ❌ `AK/SK签名校验失败`（→ brick 这个用户**存在**） |
| `brick` | brick | ✅ `code:0 success`，拿到各区域 token |

- **结论**：token 接口的 `user` 头要填**平台登录用户名**（现在是 `brick`），而不是 `act3fh878f`。
- **签名公式**：`HMAC-SHA256(secret_key, json{accessKey, timestamp, user} sort_keys)` —— 消息体也含 user，所以要**一起改**，否则签名校验失败。
- **`computeUser` 字段**：返回的 token payload 里 `computeUser` 仍是 `act3fh878f`（**计算用户 ID**），集群家目录 `/public/home/…`、作业与容器账号均不受改名影响。

修复：`~/.agt/scnet.json` 的 `user` 由 `act3fh878f` → `brick`（AK/SK 未动），旧文件备份 `scnet.json.bak_20260914_124347`。环境变量（SCNET_USER 等）未设置时，该文件为唯一凭证来源。

改动后需让 MCP 子进程用新凭证重连——它是常驻进程，凭证在启动时读入内存。配合 [MCP 配置页 · reload_mcp 热重连工具](../features/mcp-config.md) 用 `/reload_mcp scnet` 或 `/restart` 生效；在此之前 `scnet_free_card_watch` 巡检会因拿不到 token 而空转。

## HPC API 实测口径与端到端跑通（2026-09-14）

改名恢复后顺藤摸瓜，把 HPC 侧 API 一次测穿并端到端跑通（直连探测 + playwright 控制台对照）。

## fetch_centers 用错 token（已修）

CENTER 区域发现接口（`www.scnet.cn/ac/openapi/v2/center`）**必须用区域 token（如 20091）**，用 platform token（clusterId=0）恒返回 `10001 internal_error`——原实现一直用 platform token，导致 centers 恒空、hpc/ai/efile URL 全 None。修复：优先用第一个区域 token，无区域 token 才回退 platform；另加 `isinstance(it, dict)` 防御（出错时 data 是字符串）。

## HPC 接口路径修正（scnet_mcp.py 三处，实测钉死）

| action | 旧实现 | 实测 | 修正后 |
|---|---|---|---|
| queues | `GET /hpc/openapi/v2/queue?strJobManagerID=` | **404** | `GET /hpc/openapi/v2/jobs/queues?strJobManagerID=`（部分集群 data=null） |
| list | `POST /hpc/openapi/v2/jobs` | **405** | `GET /hpc/openapi/v2/jobs?strClusterIDList=<调度器ID>` |
| history | `POST /hpc/openapi/v2/history-jobs` | **404** | `GET /hpc/openapi/v2/jobs/history?strClusterIDList=<调度器ID>` |

- submit（`POST /hpc/openapi/v2/apptemplates/BASIC/BASE/job`）与 clusters（`GET /hpc/openapi/v2/cluster`）原实现即正确。
- **队列真实来源**：控制台接口 `www.scnet.cn/acx/resource/queue/manager/user-cluster-queues`（session 态，AKSK 通道拿不到）；jobs/queues 对华中一区返回 null 时，去控制台「作业提交」页查。

## 华中一区（cluster_zz_2 / 20091）作业实测口径

- 调度器：`cluster_zz_2`，SLURM，strJobManagerID=`1768486376`
- 队列：**`hx1hdnormal`**（DCU 资源"高性能计算-异构加速卡BW-1"，648 卡 total/24 free；load=busy）
- **提交必须 `ndcu>=1`**：GAP_NDCU 空串/0 报 `QOSMinGRES min tres(gres/dcu) request 0 exceeds per-job max tres limit 1`
- 集群用户：`act3fh878f`，家目录 `/public/home/act3fh878f`

**端到端验证**：2026-09-14 提交 `agt_probe_ok`（echo+hostname+sinfo）→ **jobId 834040，statC 完成**，节点 `a01r3n14`，runTime 00:00:01，输出落在 `/public/home/act3fh878f/agt_probe.out.834040`。

## 容器/AI 通道边界

华中一区 aiUrls 与 hpcUrls 同 host（`zzhpc.scnet.cn:65051`），但 `/ai/openapi/v2/*` 全 404——该网关只有 HPC。**免费容器组（武汉 138 组）API 不覆盖**，仍走 playwright 巡检（见上）。efile page-list 返回 `1001 未知异常`（URL 拼接已排除），待用文件传输时再查。

## 自定义镜像：commit 式（非 Dockerfile/制品仓库）

官方文档《镜像说明》的机制不是构建/制品仓库，而是 **commit 式**：

```
基础镜像开 Notebook → 装环境 → 更多操作→「保存镜像」→ 存入我的镜像
→ 下次开实例直接选它（单层 ≤ 15GB）
```

- Notebook 关机**秒级保存开发环境**（新功能），小改甚至不用存镜像。
- 镜像选择另有「社区镜像」tab（stable-diffusion-webui、yolov5 等 AIGC 现成镜像）。
- 「我的镜像」独立页面是需付费开通的增值服务；Notebook 自带的保存镜像无需它。

## AI 社区镜像库 · AIGC 现成镜像盘点（2026-09-14，共 895 个）

AI 社区（/ui/aihub/image）镜像库共 895 个镜像，左侧分类树含：多模态（Wan/Z-Image/Qwen-Image/hunyuan3d/StableDiffusion/3D生成/图片生成/图片编辑/**视频生成**/视频编辑）、语音（CosyVoice 等五类）、ComfyUI、IDE 工具、NLP、行业模型。**我们自己 commit 的镜像目前 0 个**（还没开过容器）。

## Notebook 免费实例实测：自定义服务端口 → 公网 URL 全链路（2026-09-14）

**容器内操作通道**：该镜像未装 SSH（提示「仅支持在线开发」），**JupyterLab 开终端**是重启/调试服务的最佳通道（本轮实测比 SSH 指令更好用）。**纯 API 等价通道（2026-09-14 打通）**：Jupyter **terminals WebSocket API** —— `wss://n-{id}.ksai.scnet.cn:58043/jupyter-forward/{id}/terminals/websocket/{name}?token=sothisai_{id}`，发 `["stdin", "命令\r"]` 即可执行任意命令（`echo`/`pkill`/`nohup` 实测可用，`sslopt={"cert_reqs": ssl.CERT_NONE}`），使「首次拉起 monitor」不再需要人点终端。详见 [SCNet 异步生产流水线 · Jupyter terminals WS](../features/scnet-async-pipeline.md)。

## 控制台纯 API 地图 + 三单实测（2026-09-14）

控制台网页操作（创建/启服务）不必依赖 playwright——**后端 HTTP API 可用 AK/SK 换来的区域 token 直接调**（`token` header，与 OpenAPI 共用同一 JWT 体系：payload 含 computeUser/clusterId/user）。

## 已验证的端点（纯 HTTP，零 cookie）

## 已验证的端点（纯 HTTP，零 cookie）

Base：`https://cancon.hpccube.com:65011`（昆山集群；华中网关是 zzhpc.scnet.cn:65051 系）

| 端点 | 方法 | 鉴权 | 实测 |
|---|---|---|---|
| `/acx/containermgt/v2/notebook/list?clusterId=11250` | GET | 区域 token | ✅ 200 实例列表全字段 |
| `/acx/containermgt/v2/notebook/{id}/notebook-url?clusterId=11250` | GET | 区域 token | ✅ 200（含 Jupyter URL、status、userToken） |
| `/acx/containermgt/notebook/v3/config` | GET | 区域 token | ✅ 200（releaseDays=30、createTimeTooLongMinutes=10 等） |
| `/acx/containermgt/instance-service/start-command` | GET | 区域 token | ✅ 200（返回 jupyter/comfyui 等启动命令模板） |
| `/acx/containermgt/port/pool/available/port/number` | GET | 区域 token | ✅ 200 |

**已封装**：以上只读端点全部封装为 MCP 工具 `scnet_notebook`（`action = list / info / url / config / start-command / ports`，纯 HTTP 直调、零浏览器），见 [SCNet 异步生产流水线 · MCP 封装](../features/scnet-async-pipeline.md)。

**实例对象关键字段**（创建 payload 的反推源）：notebookName/notebookStatus/imageName/imagePath/cpuNumber/acceleratorType(Number/Name/Mode)/ramSize/resourceGroupId/sshPassword/taskId/containerId/node/**command**（自定义服务启动指令）/**customsizePort**（服务端口）/maxNumber/**noCardMode + noCardCpuNum("0.5核心") + noCardRamSize("4GB")**（无卡模式规格！）

**Jupyter URL 实测样例**（`scnet_notebook` 返回）：`https://n-2099459694942883841.ksai.scnet.cn:58043/jupyter-forward/.../lab/tree/root/?token=sothisai_...`。

## 尚不通的端点（2026-09-14）

- `/acx/aimgt/*`（创建 Notebook 主入口：POST /aimgt/notebook、resource/adjust、clone）→ **503 Service Unavailable**（本机与浏览器上下文一致，疑平台侧或需其它入口）
- `/acx/containermgt/instance-service/task/list` 等 → `{"code":"1001","message":"jwt token is invalid"}`（需另一种 token）

## 端点清单来源

前端 JS bundle（`https://www.scnet.cn/ui/console/static/js/app.addc4147.js`，2.8MB）→ 正则提取 **437 个端点**（含 notebook/instance/service/image/HPC 全部路由）。后续要补写操作 payload，从这里继续挖或抓一次浏览器真实请求。

## 三单批量实测结果（K100_AI 64GB，￥2.53/时）

| 单 | 时长 | 产物 |
|---|---|---|
| e4bd2630（含首次权重加载） | 607s | MiniMax_H3_00001_.mp4 |
| 5b4caea7（热态） | 464s | MiniMax_H3_00002_.mp4 |
| 1b01d575（热态） | 444s | MiniMax_H3_00003_.mp4 |

**结论**：权重加载仅占约 2.4 分钟，稳态 ~7.4 分钟/单（480p/5s，4步 Turbo+EasyCache）——速度调优（low_vram/EasyCache/分辨率）是后续产能优化的重点。

## 核心发现：113 组免费 BW 64GB（限时免费）

Notebook 创建页（控制台 > 人工智能 > Notebook，`#/notebook/add`）资源列表 10 组，**113 组 hx1hgbwnormal（华中一区【A区】，异构加速卡BW 64GB，内存 59GB/CPU 15核，DTK 26.04）标「限时免费 ¥0/时」，8/8 可用（总卡数 10000+）**——正是 minimaxh3 等社区镜像要求的 Hygon BW 环境，且免费。其余：079 昆山 16GB ¥2、021 昆山 AI 64GB ¥2.53（推荐）、097 四川 ¥3.6、012 L20 ¥3、013 4090 ¥3.6、016/064/096 A800 ¥6.9-7.8。

**入口要点**：Notebook 创建页 ≠ 容器服务两处创建页——后者（新版容器组/旧版容器）最低 1 卡且无社区镜像 tab；**Notebook 创建页有「基础镜像/社区镜像/我的镜像」三个 tab**（113 组下社区镜像 tab 暂"无数据"，疑似按区域过滤，待查）。基础镜像：PyTorch/TF/JAX/MiGraphX/Paddle/SGInfer，四级级联（框架→版本→Python/OS→DTK），本次选 PyTorch 2.9.0 / py3.11-Ubuntu22.04 / dtk26.04。

## 端到端实测（2026-09-14 18:43 创建成功）

实例：`2609141843336261`（ID `2099448965488300033`），运行中，72h 上限至 09-17 18:43，¥0。快捷工具三件：**JupyterLab / 工具面板 / 访问自定义服务**（另有 SSH 指令+密码可查）。

**「访问自定义服务」= 拿公网 URL 的机制**（弹窗表单）：

```
服务端口号：    8190
服务启动指令：  python3 -m http.server 8190   （已在容器内起服务则可不填）
[启动任务] → 平台在容器内执行指令 + 代理端口 → 自动打开公网 URL
```

实测结果：点「启动任务」后自动开新标签 `https://c-2099448965488300033.zzai.scnet.cn:58043/`——`python http.server` 的目录列表页渲染成功；**外网独立进程验证 HTTP 200（nginx 反代）**。

**URL 规律**：`https://c-{实例数字ID}.zzai.scnet.cn:58043/`（zzai.scnet.cn 域，公网直达、无鉴权）。

## ComfyUI 落地结论

1. 创建 Notebook（113 组免费 BW）→ 容器内装/起 ComfyUI（或 JupyterLab 里手动起）
2. 「访问自定义服务」填端口 8190 + 启动指令（ComfyUI 启动命令）→ 拿 `https://c-{id}.zzai.scnet.cn:58043/`
3. 同 URL 直接 POST `/prompt`（ComfyUI API 无鉴权）→ `/history/{id}` 轮询 → `/view` 取产物——8000 实例在 CNB 上的 ComfyUI 自动化经验整套平移；2026-09-14 已封装成本地批量客户端 [`tools/scnet_comfy_client.py`](#scnet_comfy_clientpy--本地批量任务客户端2026-09-14)

落地待办（已推进，2026-09-14）：113 组下社区镜像 tab 无数据，但该镜像在**华东一区 021 组**（昆山 AI 64GB，¥2.53/时）可用——已选定 director-v2 版本镜像、跨区同步中，见下节「minimaxh3-director-v2 落地」。

## minimaxh3-director-v2 落地：镜像跨区同步 + scnet_img_sync 巡检（2026-09-14）

把「ComfyUI 落地结论」从纸面推进到实际开卡，本轮完成到「等镜像同步」这一步。

## 选型与资源组

| 项 | 值 |
|---|---|
| 镜像 | `minimaxh3-comfyui-easycache-turbo-lora-docker`（**minimax-h3-director-v2** 版） |
| 内容 | 全部权重 + 导演台工作流 + 3 张参考图（78.93GB） |
| 资源组 | **021 昆山 AI 64GB**，¥2.53/时 |
| 版本坑 | 021 组下该镜像有 4 个版本，旧的 `v1.0.3`（45GB）在 AI 卡上 **disabled**——选最新的 director-v2 |
| 限制 | 提示「当前镜像未安装 SSH，仅支持在线开发」——符合预期，走 JupyterLab + ComfyUI WebUI |

## 镜像跨区同步（阻塞点）

021 组镜像需**跨区同步**（78.93GB 内网搬运，提示「同步镜像时间较长」），同步完成前无法创建实例。

## scnet_img_sync 巡检任务（自动续跑）

挂定时后台任务 `scnet_img_sync`（每 5 分钟，[add_schedule](background-scheduler.md) 机制）：

1. 检查镜像同步状态
2. 完成后自动回创建页（021 组 + **我的镜像** tab）→ 创建 Notebook → 开机
3. 「访问自定义服务」填端口 **8190** + 启动指令
4. 拿 `https://c-{id}.zzai.scnet.cn:58043/` → 验证 HTTP 200 → 汇报

## 本地批量任务客户端（已备好，待实例就绪）

`tools/scnet_comfy_client.py`——本地向容器批量布置任务并下载产物，走 ComfyUI 官方 HTTP API（enqueue/poll/download 全套 + 参考图上传接口预留）：

```bash
# 单次：提交工作流 → 轮询完成 → 自动下载产物到本地
python tools/scnet_comfy_client.py --url https://c-xxx.zzai.scnet.cn:58043/ \
    --wf workflow_api.json --outdir ./scnet_outputs

# 批量：同一模板换提示词逐个 enqueue（如换 3 个镜头描述）
python tools/scnet_comfy_client.py --url ... --wf ... \
    --batch 'text::a cat::a dog::a car' --edit-node 6 --outdir ./scnet_outputs
```

## scnet_comfy_client.py · 本地批量任务客户端（2026-09-14）

**职责**：把本地 workflow（API 格式 JSON）提交到 SCNet 容器里的 ComfyUI，轮询完成并下载产物；支持同一模板批量换字段值。

**关键能力**
- 走 ComfyUI 官方 HTTP API：`POST /prompt` → `GET /history/{id}` 轮询 → `/view` 取产物
- 参考图上传接口预留（对接 H3 的 9 图/3 视频/3 音频参考能力）
- `--batch '字段::值1::值2' --edit-node N`：同一 workflow 模板，逐值改写指定节点字段后逐个 enqueue
- `--outdir`：产物落本地目录

**与其它模块的关系**：SCNet 侧对应「[访问自定义服务](#notebook-免费实例实测自定义服务端口--公网-url-全链路2026-09-14)」拿到的公网 URL；自动化经验平移自 8000 实例在 CNB 上的 ComfyUI 玩法。注意 ComfyUI API 无鉴权，URL 即凭证，勿外泄。

## 生视频镜像（视频生成分类，4 个，全部 DCU/BW 适配）

| 镜像 | 作者 | 特点 | 开机可用 |
|---|---|---|---|
| **minimaxh3-comfyui-easycache-turbo-lora-docker** | acqe2rbhn4 | H3 Ref2VA + EasyCache + 4步 Turbo LoRA；**内置全部权重（43.25GB，镜像共 78.93GB）+导演台工作流+3 张参考图**；ComfyUI 0.31.0，端口 8190；运行环境 Hygon BW/gfx936/DTK 26.04；支持 9 图+3 视频+3 音频参考 | ✅ 真·开箱即用，无需下载模型（⚠️ 勿挂载空目录到 /root/ComfyUI/models，会遮挡内置权重） |
| jupyter-minimax_h3 | eq1qe | H3 int8 量化 + 加速 LoRA，WebUI 适配单卡 | ✅（权重内置或自动下载，见详情） |
| jupyter-ltx2.5 | eq1qe | LTX2.5，单卡 1920×1088×5s，降分辨率增时长 | ✅ |
| comfyui-short-drama-openclaw | Icylin | 短剧制作：OpenClaw + Krea2 关键帧 + LTX2.3 工作流 + 云端点 | ✅ |

## 生图镜像（图片生成分类 3 个 + 热门相关）

| 镜像 | 作者 | 特点 |
|---|---|---|
| **comfyui_hygon_boost** | ac3jw5udsq | 原生 ComfyUI，**专为白嫖的海光 BW 卡提速**（Anima/Krea2 Turbo int8 加速明显） |
| **jupyterlab-zimage-webui** | 智能时代 | Z-Image-Turbo 生图，中文提示词（热门，115h 运行时长） |
| jupyterlab-qwen-image-edit | 智能时代 | Qwen-Image-Edit 20B 图像编辑（文本渲染扩展） |
| anima-lora-train-dcu | SqrtZ | Anima 二次元 LoRA/LoKr 训练（DCU 版 flash_attn 预编译） |
| anima-standalone-trainer-hygon | ac3jw5udsq | Anima 独立训练器（fa2/xformers/bnb） |

## ComfyUI 整合包（ComfyUI 分类，3 个）

- **comfyui-dcu-bigbomb**（BigBomb）：ComfyUI 0.33.3+常用节点，主打 Flux klein 9b 工作流——**需自备模型**（下载后连目录启动即用）
- **comfyui-harness-minimaxh3**：H3 视频生成懒人包，内置 4 种社区方案+官流，**模型运行时自动下载**
- **comfyui-withopenclaw**（Icylin）：Krea2 + MiniMax H3 模型和工作流，**开箱即用**

## 智能体（免容器，最省事）

- **MiniMaxH3视频生成--光影智创（LumaCraft）**：文生图+文生视频+图生视频一站式工作台，"无需配置模型环境"（22.5k 下载）
- **月光音乐盒**：AI 音乐/歌曲生成（专家/快速双模式）

## 使用入口

镜像详情页「**快速开发**」按钮一键创建开发实例；或创建容器时填 DCU 镜像地址（如 `appstore.scnet.cn:5000/aihub/dcu/acqe2rbhn4/...:minimax-h3-director-v2`）。注意：容器组创建页的「基础镜像」只有训练/推理框架（JAX/PyTorch/TF/DeepSpeed/Xinference/SGInfer 等 10 类），AIGC 镜像在 AI 社区侧。硬件匹配：H3 全家桶需 64GB BW 卡（免费武汉 138 组 BW 匹配；16GB 小卡跑不动 H3，用 zimage 生图或 LumaCraft 智能体）。

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

## 免费容器组巡检定时任务（scnet_free_card_watch，2026-09）

免费 K8s 容器组（武汉 138 组，¥0）是本平台唯一的免费算力入口，但长期满载。已挂**定时后台任务** `scnet_free_card_watch`（[add_schedule](background-scheduler.md) 机制，`src/background.py` 调度，到点弹后台通知轮唤醒）——到点自动巡检，抢到空位即自助创建。

巡检流程（playwright 复用扫码登录态，全自动）：

1. 打开创建容器实例页：`https://www.scnet.cn/ui/console/index.html#/container-service/container-group/add`
2. 等「创建容器实例」出现后，读「单机可用卡数」：`innerText` 正则 `单机\s*(\d+)\s*\/\s*(\d+)`（当前值 `0/2`）

分支：

- **可用 > 0（抢到）**：按既定配置直接创建——卡数=1、镜像 `PyTorch/2.9.0/py3.11-Ubuntu22.04/dtk26.04`、协议已勾、其余默认；创建成功后跑 `python C:\Users\vgp77\.agt\mcp\scnet\scnet_mcp.py --selftest` 验证区域 token 解锁；一切就绪后 `cancel_schedule` 自停本任务。
- **仍为 0**：一句话汇报结果，不做任何多余操作，任务留到下轮。

**当前状态（2026-09 巡检）**：武汉 138 组仍 **0/2 可用（满载）**，任务保持挂机等空位。

> `scnet_mcp.py`（`~/.agt/mcp/scnet/`）：SCNet 配套独立脚本（非 workspace 内，用户侧），`--selftest` 用于创建成功后验证「区域 token 解锁」是否生效（首次开卡的基础条件）。

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

**2026-09-14 落地进展（已实跑）**：021 昆山实例上 ComfyUI 已跑通并**出片**（`MiniMax_H3_00001_~00006_.mp4`），异步生产流水线（画布转 API + 容器主动回调本机）已部署并**无人值守闭环持续运行**（2026-09-14 21:30 起，5 单批量 **3/5 已自动回传**落 `scnet_inbox/`，连续三单零人工零失败）；monitor 因**单端口约束**升级为「反代 + 监控二合一」，并进一步**常驻化 + 任务 HTTP 化**（v3：`POST /monitor/add` 加任务，不必进容器）；容器内执行命令经 **Jupyter terminals WebSocket API** 打通（纯 API，首次拉起亦可自动化）；另备本机兜底轮询 `tools/scnet_watch_batch.py`。热态出片实测 **稳态 443-444s/单（≈7.4 分，约 8 单/小时）**；产物清单 `scnet_outputs/manifest.json` 记 prompt_id/seed/时长便于复现。详见 [SCNet 异步生产流水线](../features/scnet-async-pipeline.md)。

## 相关页面

- [SCNet 异步生产流水线](../features/scnet-async-pipeline.md) — 画布转 API 转换器 + 容器主动回调 + 批量出片打法
- [配置体系与模型调优](config-and-models.md)
- [本地模型](local-models.md)
- [运维、可观测性与排障](ops.md)

