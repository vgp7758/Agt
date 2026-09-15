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

## Notebook 无卡模式镜像构建：015 实例 agt 组网 + 环境持久性实测（2026-09-14）

**「无卡装环境、有卡跑推理」路线全链路跑通（¥0）**：015 实例（昆山，minimaxh3 那台）无卡模式开机 → 容器内装 agt → agt-web 公网直通 → 本机 remote 组网接管 → 「保存镜像」被平台限制，但实测**关机环境保存已覆盖该需求**。

### 无卡模式开机：startType "no-card"

此前 cookie 通道抓到的 [restart 端点](#关机与生命周期-apicookie-通道)扩一参即得：

```
POST https://www.scnet.cn/acx/aimgt/notebook/restart
{"startType":"no-card","notebookId":"…","taskId":"…","clusterId":11250}
```

- 规格 = list 字段早已暴露的 `noCardCpuNum:"0.5核心" / noCardRamSize:"4GB"`——0.5 核 4GB 免费 CPU 实例，装环境/整理文件全程 0 卡时
- 第二次开机实测通过；要切回有卡推理，再 restart 一次 `startType:"normal"` 即可

### 容器内 agt 组网（agt-web 8191 → 公网 → remote 接管）

1. `pip install agt-agent`（镜像 v2 自带 0.28.0 → 当晚升 0.28.1，见 [v0.28.1](../releases/v0.28.1.md)）
2. `agt-web` 起在 **8191**（实例自定义服务端口 `customsizePort:8191`）→ 平台自动映射公网 `https://c-2099459694942883841.ksai.scnet.cn:58043`（**ksai 域**；021 出片实例是 zzai 域——两种域名并存）
3. 本机 `remote_connect` → **134 工具**入列；工具路由实测：`run_python(remote_instance_id="scnet")` → `[remote:scnet]` 在容器里执行 ✅——主 Agent 无需 scnet MCP 也能直接操作容器（组网机制见 [multi-instance](../architecture/multi-instance.md)）

### models.json 注入：upload→mv 两段式（主模型真源在 main.yml）

Jupyter Contents API **不让写 `.agt` 隐藏目录**——先 upload 到可见临时路径、再 shell `mv` 进 `~/.agt/`。内容在本机生成后 base64 传输。

**16 providers 整份 clone（2026-09-14 晚）**：本机 `~/.agt/models.json` 整份 clone（16 providers + default + **deepseek 官方端点的缓存经济学参数** `fold_target_ratio: 0.4 / detail_step: 0 / requires_reasoning_in_history`）落 `scnet_agt_models_full.json`；agt-web 重启后生效。容器里的 agt（scnet 组网节点）由此成为「容器管家」——可在 30 天保留期内自主打理模型补齐/寻源/生产任务监控。

**主模型真源在 main.yml，不在 models.json（2026-09-14 晚实测教训）**：改 models.json 的 `default` 后管家模型没变——主 Agent 的模型声明在 `/root/.agt/main.yml`（当时 `model: glm`），**装配 DSL 优先级高于 models.json default**。修法：sed 把 main.yml 改 `model: deepseek` + 清掉旧测试 session，重启 agt-web 生效。

**容器管家定档 deepseek-flash（2026-09-14，用户裁定）**：「容器里的实例还是走 api 吧，可以用 deepseek-flash，相对稳定，用的比较慢」——慢=折叠触发少=缓存好，正合管家定位（守夜/巡检/批量任务监控，不追求首 token 快）。当前 134 工具入列、ready。

### 环境持久性实测：关机 → 重开机，容器层增量全在

stop（`saveEnv:true`）→ restart 后验证：pip 包、热修的 `tools.py`、`models.json` **一个不少**——「关机环境保存」是真实的。**结论：试用实例本身就是「活镜像」**——只要不释放实例，环境跟着实例走，开机即用（呼应[自定义镜像 commit 式](#自定义镜像commit-式非-dockerfile制品仓库)的关机秒级保存机制）。

### 保存镜像被平台限制（manualSaveImage: false）

| 尝试 | 结果 |
|---|---|
| 页面菜单「保存镜像」 | 点击**完全静默**（无请求无弹窗）——前端拦截 |
| API 直调（字段全部摸清：containerId/containerType/node/name/tag/fromPath/notebookId/clusterId） | 参数合法 → `Internal Server Error`——后端也拒 |
| 根因 | list 返回 `manualSaveImage:false`——**试用实例禁用手动保存镜像** |

需求实际已被「关机环境保存」覆盖（见上节）；真要跨实例分发镜像时，找平台开通 manualSaveImage 或换正式付费实例操作。

### 资源组计费澄清：113 组免费 ≠ 50 卡时（用户问询钉死）

| 资源组 | 卡型 | 计费 | 与 50 卡时试用额度 |
|---|---|---|---|
| **113 组**（华中一区，免费 BW） | BW 64GB | **¥0/时 免费** | **不消耗**——独立免费组 |
| **015 实例**（昆山，minimaxh3 台） | DCU/L20 | ¥2/时 | **消耗**试用额度 |

- 113 组直接正常开机就免费（15核/59GB，比无卡 0.5核/4GB 强 30 倍）——**不必对它用无卡模式**；无卡模式是给付费/试用组省卡时用的
- **跨组坑**：BW 与 DCU 的 GPU 侧 Python 环境（torch 等）不通用；但**权重文件与卡无关**，且同一账号家目录 `/public/home/scnrilsyy5` **同集群内跨实例共享**（⚠️ 2026-09-14 morning_wake 轮口径修正：**跨集群不共享**——113 武汉集群有独立家目录，昆山下的权重那边看不到，各自现下；此前「跨实例共享」表述仅对昆山 015/021 成立）
- **分工策略（⚠️ 2026-09-14 晚修正）**：「113 当免费下载机」**已推翻**——113 网络出口不通（见下节），下载机职责归 015 无卡容器（35~40 MB/s）；现行分工 = 015 无卡装环境 + 下模型（¥0）→ 015 有卡才推理（只花真实卡时）；113 只当「离线免费推理卡」看待（模型经 SFTP 慢传 5.8h，或等外网开通）

当前状态（2026-09-14 晚）：015 实例无卡常挂（¥0，agt-web 公网 ready + 组网在线）——管家模型 **deepseek-flash**（走 API，用户裁定）+ 家目录 comfy-models **80G 模型库全部就绪**（`dl.done`）；113 已关机（环境保存）；镜像内容 = ComfyUI + H3 + agt 0.28.1 + models.json，开关机不丢。

## 模型全家桶下载：015 无卡容器 → 家目录共享盘（2026-09-14，scnet_dl_all.sh）

morning_wake 轮（用户指令「把需要下载和安装的东西都折腾好」）启动：视频游戏管线所需模型全家桶在 **015 无卡容器**（¥0，0.5核/4GB）批量下载，实测带宽 **35~40 MB/s**。

**落点设计**：全部落昆山家目录共享盘 `/public/home/scnrilsyy5/comfy-models`——**关机不丢、换实例可用**（呼应[无卡环境持久性](#notebook-无卡模式镜像构建015-实例-agt-组网--环境持久性实测2026-09-14)）；容器侧 `extra_model_paths.yaml` 已配好（家目录 comfy-models + 镜像内置 `/home/models` 双挂载）。⚠️ 家目录共享仅限**昆山集群内**——113（武汉）不共享，见[下节注意点](#113-免费卡时-llm-推理侦察vllm-镜像盘点--实测方案2026-09-14)。

**五个批次**（`scnet_dl_all.sh`，workspace 根，104 行）：

| 批次 | 内容 | 去处（comfy-models/ 下） |
|---|---|---|
| A | Qwen-Image 文生图三件套：fp8 unet + qwen2.5-vl_7b_fp8 文本编码器 + vae（ModelScope `Comfy-Org/Qwen-Image_ComfyUI`） | diffusion_models / text_encoders / vae |
| B | Qwen-Image-Edit 2511：int8_convrot + vae_fp16（ModelScope `Comfy-Org/Qwen-Image-Edit_ComfyUI`） | diffusion_models / vae |
| C | IndexTTS-2.5 全套：gpt.pth(3.1G) + s2mel + codec + qwen0.6bemo4-merge 五小件（ModelScope `IndexTeam/IndexTTS-2.5`） | indextts/ |
| D | H3 文戏缺件五件：fl2va 主模型 / qwen3vl_32b int8 文本编码器 / turbo_4step / FeiHou Remix / latent_upscaler_3d_fp16——**git-lfs 方式**（`GIT_LFS_SKIP_SMUDGE=1` 浅克隆 `cnb.cool/fuliai/minimaxH3` → `git lfs pull --include` 逐个拉） | 按文件名 case 归位 loras / upscale_models / text_encoders / diffusion_models |
| E | custom_nodes 四件：VHS / LayerStyle / rgthree / KJNodes（github 直连已验证 200） | `/root/ComfyUI/custom_nodes/`（容器内，非共享盘） |

**脚本幂等设计**：`dl()` 封装 `curl -sfL -C - --retry 3`（断点续传）× 30 轮重试；`dl.log` 全程留痕（OK/RETRY/FAIL/PLACED 分行带时间戳与体积）；已完成的文件秒过，可反复跑；批次 E 已存在的节点目录 SKIP；全部批次结束 `echo done > $M/dl.done` 哨兵文件。git-lfs 缺失时自动 apt 安装（失败仅 WARN 不阻断 A/B/C）。

**巡检闭环**：25 分钟周期定时任务盯进度；`dl.done` 出现后汇总 OK/FAIL 清单报用户；FAIL 项自动换源补下。

### 下载批次收官：80G 全部完成（2026-09-14 晚，dl.done）

晚间巡检确认五批次**全部完成**（约 80G），`dl.done` 哨兵已落，`extra_model_paths.yaml` 双路径挂载就绪——**下次开有卡模式即可直接跑生产**，无需再下载。

| 批次 | 状态 | 明细 |
|---|---|---|
| A 文生图 | ✅ | Qwen-Image fp8 unet 19.5G + qwen2.5-vl 8.8G 文本编码器 + vae |
| B 图片编辑 | ✅ | Qwen-Image-Edit 2511 int8_convrot 19.6G（vae 软链复用）+ **6 个 2509 系编辑 LoRA**（重打光/换背景/多角度/Anything2RealAlpha/Fusion/Light-Migration——修立绘场景正合适） |
| C 语音 | ✅ | IndexTTS-2.5 全套（gpt.pth 3.26G / s2mel / codec / qwen0.6b-emo4） |
| D 视频增强 + H3 | ✅* | realesr-animevideov3 真视频超分 + RIFE 插帧 ✅；H3 文戏五件部分到位——**个别上游断供件待寻源** |
| E custom_nodes | ✅ | VHS-VideoHelperSuite / LayerStyle / rgthree / KJNodes |

落点 `/public/home/scnrilsyy5/comfy-models/`（61PB 共享盘，昆山集群内跨实例可用）。

## 113 免费卡时 LLM 推理侦察：vllm 镜像盘点 + 实测方案（2026-09-14）

用户指令第二步「试试去 113 的免费 50 卡时上推理看看」。纯 API 探查（不花卡时）已完成镜像盘点，实测方案已定。

**镜像盘点**：BW 集群 = **clusterId 20091**，镜像库共 **93 个**，LLM 推理三候选：

| 镜像 | 用途 | 体积 |
|---|---|---|
| **vllm 0.15.1-dtk26.04** | LLM 推理首选（SSH 形态） | 8.5G |
| sglang 0428 | 备选推理框架 | 14.7G |
| jupyterlab-pytorch 2.9.0-dtk26.04 | 通用兜底（vllm 起不来时 transformers 直跑） | 6.5G |

**实测方案**（等下载批次收尾后执行）：113 组开 1×BW 64GB（免费，见[计费澄清](#资源组计费澄清113-组免费--50-卡时用户问询钉死)）→ ModelScope 直下 Qwen2.5-7B-Instruct-AWQ（5.7G，113 侧带宽分钟级）→ `vllm serve` 起服务 → curl 实测生成速度（token/s）→ **测完立刻关机**。若能跑出像样速度，50 免费 BW 卡时即可当 LLM 推理池用。

**两个注意点（本轮口径）**：

1. **模型要现下**：113 是武汉集群，与昆山家目录 `/public/home/scnrilsyy5` **不共享**——`comfy-models` 全家桶昆山专属，113 侧用不上；但 7B AWQ 仅 5.7G，现下无所谓（家目录共享口径修正详见[计费澄清节](#资源组计费澄清113-组免费--50-卡时用户问询钉死)）。
2. **用完即关**：BW 实例开机即开始计（保守口径；与[计费澄清](#资源组计费澄清113-组免费--50-卡时用户问询钉死)「113 ¥0 不消耗试用额度」并存——反正实测流程压缩到最短，开→测→关一气呵成）。

**节奏**：下载批次完成（`dl.done`）且无 FAIL → 自动开 113 → 7B 推理实测 → 关机 → 带 token/s 数据汇报；有 FAIL 则换源补下后再走同流程。015 无卡实例全程挂着（操作台 + agt 组网节点，¥0）。

### 创建卡点：headless 镜像选择器失效 + API 1004（2026-09-14 晚）

113 组免费实例的创建在自动化侧卡住，需用户手点 4 步。

- **API 直调三轮**全被 `1004 参数不能为空` 拦（不指明字段）；创建字段已全部挖出——`goodsId=1005861210643496960`、`resourceGroupId=hx1hgbwnormal`（113 组）、加速器名「异构加速卡BW」、镜像 path/size 全套——但某个隐藏必填字段尚无法定位。
- **headless 浏览器镜像选择器失效**：Element UI 的 cascader/卡片 popover 在 headless 下试了真实鼠标、dispatchEvent、Vue 实例直调，全部无法 commit。

**需用户手点（4 步，2 分钟）**，起好后 Agent 全自动接管（vLLM 测速 → 关机）：

1. scnet.cn 控制台 → Notebook → 创建Notebook
2. 加速卡：选「113 组 hx1hgbwnormal · 限时免费」卡片（BW 64GB），卡数 1
3. 开发镜像 →「社区镜像」tab → 选 **jupyterlab-qwen3-openwebui**（Qwen3 全家桶，开箱即用、BW 兼容、自带模型）
4. 立即创建 → 等 Running

> 此前镜像盘点定的 vLLM 方案顺延：jupyterlab-qwen3-openwebui 自带模型，可免去「现下 7B AWQ」一步。

### 113 网络出口不通 + SFTP 0.8 MB/s——推理验证搁置、关机收尾（2026-09-14 晚）

实例创建成功（用户手点四步），SSH 通道 `ssh.zzai.scnet.cn:10300`（root + sshPassword）实测可连。本轮验证结果与原方案（ModelScope 现下 7B → vllm serve）预期相反：

| 验证项 | 结果 |
|---|---|
| **无卡模式** | ✅ 可用——`stop(saveEnv) → restart(startType:"no-card")`，实测 torch 计数=0 完全不占卡 |
| **网络出口** | ❌ **不通**——有卡/无卡模式下 ModelScope / pypi / github 全部连接失败（000） |
| **SFTP 通道** | ✅ 通但仅 **0.8 MB/s**（平台 SSH 跳板限速；100MB 上传实测 123.8s） |

**定性**：网络不通**不是无卡模式限制**（有卡模式同样不通）——疑 113 免费试用组**本来就没配外网出口**；对照 015 付费组 35~40 MB/s 全通。「113 当免费下载机」的分工策略就此推翻。

**SFTP 经济账**：Qwen3-8B（ModelScope repo files API 实测：16 文件、>100KB 共 8 个、16.4G）经 0.8 MB/s 跳板要 **~5.8 小时**——本机已列好文件清单 + 生成 curl 直链下载脚本（`dl_qwen3_113.sh`，未执行），**推理验证搁置**。

**处置**：113 **已关机（saveEnv，环境保留）**，要用随时拉起（无卡/有卡均可）；当日总消耗 ≈ 0.4 卡时（首开 8min + 网络验证 25min）。

**后续两个选项（待用户定）**：① 哪天挂 SFTP 后台慢慢传（5.8h 无人值守，不占人）；② 问平台能否给 113 组开通外网——开了就是「免费 LLM 推理节点」，vllm 方案原样复活。

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

## 代码仓库（2026-09-14 起独立为 scnet-mcp）

**SCNet MCP 的代码已从 agt 仓库迁出，独立为私有 repo（2026-09-14）**：

| 项 | 值 |
|---|---|
| 远端 | `https://codeup.aliyun.com/618914e04d2b371c479a6963/vgp7758/scnet_mcp.git`（阿里云 Codeup，**private**） |
| 本地 | `D:\Projects\scnet-mcp` —— **真源**；`~/.agt/mcp.json` 的 `scnet` 直接指向这里的 `scnet_mcp.py` |
| 内容 | `scnet_mcp.py`（9 工具）+ `monitor.py`（容器侧模板，唯一真源）+ `docs/`（20 篇平台 API 参考）+ README + pyproject |
| 凭证 | AK/SK 走 `~/.agt/scnet.json` 或环境变量；**cookie 走 `~/.agt/scnet_cookie.json`**（已 gitignore，永不入库） |

**在别的机器 / 别的 agt 实例启用**：
```bash
git clone https://codeup.aliyun.com/618914e04d2b371c479a6963/vgp7758/scnet_mcp.git
pip install mcp websocket-client
# mcp.json: "scnet": {"command": "python", "args": ["<clone>/scnet_mcp.py"]}
# 导出浏览器 cookie → ~/.agt/scnet_cookie.json（JSON 数组：name/value）
# 然后 reload_mcp_server scnet
```

**历史沿革**：monitor 模板走过「repo `tools/scnet_monitor.py` ↔ `~/.agt/mcp/scnet/monitor_template.py` 双份手工同步」→「内联进 `scnet_mcp.py`」→「独立 repo 后拆回同目录 `monitor.py` 文件（`_monitor_source()` 读它）」——**最终形态既真源唯一又可读可 diff**。agt 仓库里 `tools/scnet_*.py` 已删除（git 历史可回溯）。

## 关机与生命周期 API（cookie 通道）

**关机 / 开机（stop / restart）——三套路由里只有一套能用，2026-09-14 抓包实锤**

浏览器真实请求（playwright request 监听，点下确认后抓到）：

```
POST https://www.scnet.cn/acx/aimgt/notebook/stop      # 关机
{"id":"2099459694942883841","saveEnv":true,"clusterId":11250}

POST https://www.scnet.cn/acx/aimgt/notebook/restart    # 开机（注意叫 restart 不叫 start）
{"startType":"normal","notebookId":"2099459694942883841","taskId":"2099503338633965570",
 "clusterId":11250,"teamSharingPath":"/public/share/act3fh878f"}
```

| 曾经试错的写法 | 结果 | 为什么错 |
|---|---|---|
| `POST cancon.hpccube.com:65011/acx/containermgt/notebook/task/actions/stop?ids=<id>` | `816822 任务不存在！` | 老 v1 路由；且 `ids` 要的是**当前 taskId**（每次开机会换） |
| `POST cancon…/acx/appcenter/userAsset/notebook/<id>/stop` | `503` | appcenter 路由不在该网关 |
| `POST www.scnet.cn/acx/appcenter/userAsset/notebook/<id>/stop` | `15011 数据资源不存在` | 域名对了但端点不对 |
| `POST www.scnet.cn/…` + 区域 token | `401 用户未登录!` | **网页网关只认 cookie**，不吃 AK/SK 换的 token |

要点：

1. **域名**：网页控制台走 `www.scnet.cn`（cookie 鉴权）；`cancon.hpccube.com:65011` 是 HPC 老网关（只对 `containermgt/v2` 那套**读**接口有效）。
2. **鉴权**：只认 **cookie**（导出在 `~/.agt/mcp/scnet/scnet_cookie.json`，15 键含 `token`/`Token`/`jsessionid`）。**可拷到其它机器/实例复用** —— 这正是"其它实例不配 playwright 也能关机"的解法。
3. **taskId 是动态的**：每次开机都生成新 taskId（本次 `2099459695286816769` → `2099503338633965570`），所以 `restart` 前必须从 list 现取，不能缓存。
4. **参数**：stop = `{id, saveEnv, clusterId}`；restart = `{startType:"normal", notebookId, taskId, clusterId, teamSharingPath?}`。昆山 `clusterId=11250`。
5. **接线前**：点开机时会先 `POST /aimgt/notebook/check-image-exists`（**form-urlencoded**，`id=<实例id>`）校验镜像。
6. **已固化**：`scnet_notebook(action="stop"|"restart", payload={"id": "...", "save_env": true})`。实测 stop：`{"code":"0","msg":"success"}`（Running → Shutting → Terminated）；对已关机实例：`{"code":"817140","msg":"当前Notebook已经停止！"}`（可作幂等判定）。restart 自动从 list 取最新 taskId。

**按钮 DOM 定位**（playwright 自动化用）：Notebook 列表页「操作」列（第 8 个 td）内 2 个 `a.el-tooltip`——**第一个的运行/停机切换按钮**，靠 svg 的 `clip-path url(#…)` 判类型（`instance-start__` / `instance-shut-down__`），第二个是 `instance-more__`。两个坑：① 按钮需 **hover 行** 才可见；② 页面残留 `.el-dialog__wrapper`（例如点过「设置定时关机」）会**拦截指针事件**，hover/click 全部超时——先 `display:none` 清掉再操作。确认框：`.el-message-box:has-text("确认关机"/"确认开机")` 内的「确认」。

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

**2026-09-14 落地进展（已实跑）**：021 昆山实例上 ComfyUI 已跑通并**出片**（`MiniMax_H3_00001_~00007_.mp4`），异步生产流水线（画布转 API + 容器主动回调本机）已部署并**无人值守闭环持续运行**（2026-09-14 21:30 起，5 单批量 **4/5 已自动回传**落 `scnet_inbox/`，仅剩 `fe94db7b` 在跑，连续四单零人工零失败）；monitor 因**单端口约束**升级为「反代 + 监控二合一」，并进一步**常驻化 + 任务 HTTP 化**（v3：`POST /monitor/add` 加任务，不必进容器）；容器内执行命令经 **Jupyter terminals WebSocket API** 打通（纯 API，首次拉起亦可自动化）；另备本机兜底轮询 `tools/scnet_watch_batch.py`。热态出片实测 **稳态 443-444s/单（≈7.4 分，约 8 单/小时）**；产物清单 `scnet_outputs/manifest.json` 记 prompt_id/seed/时长便于复现。详见 [SCNet 异步生产流水线](../features/scnet-async-pipeline.md)。

**monitor 真源内联（2026-09-14 晚）**：monitor v3 源码已内嵌进 `~/.agt/mcp/scnet/scnet_mcp.py` 的 `MONITOR_SOURCE` 常量（`MONITOR_VERSION="v3-2026-09-14"`，逐字节等于原脚本），**单文件自包含**——`~/.agt/mcp/scnet/` 现只剩 `scnet_mcp.py`（61,312 B）+ `scnet_cookie.json`（1,817 B，敏感）+ `docs/`（20 份平台 API 参考）。旧的双份副本 `tools/scnet_monitor.py`（已从 repo 删除，commit `ea1b031`）与 `monitor_template.py`（已从 MCP 目录删除）消失，双源同步问题从根上消除；另清掉 9 个 `.bak`。给新实例开 SCNet 能力 = 拷 2 个文件 + mcp.json 加一行 + `reload_mcp_server scnet`。

**方向（用户已表态，未实施）**：SCNet MCP 是**独立产品**，塞进 agt repo 属错配——考虑给它**自己的 repo**（`scnet-mcp`：`scnet_mcp.py` + `monitor.py` 独立文件 + `docs/` + README + `pyproject.toml` 支持 pip/uvx 分发）。独立 repo 后内联可拆回独立文件（那时只有一份真源且可读可 diff）。**隐私红线**：`scnet_cookie.json` 永不入库；AK/SK 走 `~/.agt/scnet.json`/环境变量（现状已无硬编码）；代码里已核查无实例 id / 隧道 URL / 回调 token；唯一待中性化的是注释里的实测用户名。待用户定 repo 名与可见性。

## 相关页面

- [SCNet 异步生产流水线](../features/scnet-async-pipeline.md) — 画布转 API 转换器 + 容器主动回调 + 批量出片打法
- [配置体系与模型调优](config-and-models.md)
- [本地模型](local-models.md)
- [运维、可观测性与排障](ops.md)

