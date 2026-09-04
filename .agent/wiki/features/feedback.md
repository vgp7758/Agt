# /feedback 反馈通道 · 本地落盘 + 飞书 webhook 推送 + 探针过滤

> 代码：`src/feedback.py`（纯函数模块，零引擎依赖）

## 职责

用户反馈提交（CLI / WebUI / Agent 三入口共用，与 download.py 对称：纯函数 + 命令/工具/前端共用）：

1. **本地落盘（永远做）**：`~/.agt/feedback/<时间戳>_<类型>.json`——兜底，绝不丢
2. **飞书 webhook 推送（可选）**：webhook 启用且配了 URL 时，组装交互卡片 POST 推送，实时到作者手机
3. **探针过滤（2026-09 新增）**：推送前判 `_looks_like_probe`，疑似自动化扫描器探针只落盘不推送（详见下文）

## 配置

- `~/.agt/feedback.json`：`{webhook_url, enabled}`；`webhook_url` 留空用随包 `DEFAULT_WEBHOOK_URL`（作者飞书 incoming 机器人）；`enabled: false` 只落盘不上报（隐私可关）
- `AUTHOR_CONTACT`：作者联系方式（微信/邮箱/GitHub），`author_contact_str()` 拼接后附在反馈成功文案尾部，方便用户后续直接联系

## 关键函数

| 函数 | 说明 |
|---|---|
| `submit_feedback(kind, content, contact, env_info, agent)` | 主入口。kind 不在 `VALID_KINDS`（bug/建议/问题/赞美）归为「建议」；content 空报错不写。流程：落盘 → 探针判定 → enabled/url 检查 → 推送 |
| `_gather_env` | 组装环境信息。显式 `env_info` 优先（传 `{}` 表示不带环境），否则现采 version + os（agent 场景附加 model） |
| `_save_local` | 落盘；kind 文件名只过滤路径非法字符、保留中文可读性 |
| `_build_feishu_card` | 飞书交互卡片；kind→配色/emoji（bug 🐞 red / 建议 💡 blue / 问题 ❓ orange / 赞美 ❤️ green） |
| `_post_feishu` | POST（timeout=6），失败不抛；飞书成功判定 `code=0` 或 `StatusCode=0`（新旧接口兼容），失败返回 `(False, 原因)` |
| `make_feedback_tools(agent)` | Agent 自主反馈工具；闭包同名遮蔽用 `globals()["submit_feedback"]` 显式取模块级（同 download.py 手法） |
| `_looks_like_probe` | 探针判定（见下节） |

## 探针过滤：PyPI fuzz 扫描器骚扰治理（2026-09，commit b6d3ea2）

### 现象与定性

每次 PyPI 发布后，作者飞书收到这样的推送：

```
💡 建议
/tmp/pp-fuzz/probe
联系方式：(未留)
环境：version=0.22.3 · os=Linux-4.19.0-gvisor-x86_64-with-glibc2.41
```

**定性：不是用户，是自动化安全扫描器**——订阅 PyPI 新版本 → 沙箱拉包 → 自动遍历命令探测出网通道。`/feedback` 会真实 POST 到作者飞书 webhook，等于给探针回了「探测成功」执，所以每次发布必到。

### 证据链（四条）

| 证据 | 含义 |
|---|---|
| `os=Linux-4.19.0-gvisor-x86_64` | gVisor 是 Google 用户态内核沙箱（Cloud Run / 批量分析环境专用），不是真人桌面 |
| 内容 `/tmp/pp-fuzz/probe` | 字面就是「模糊测试探针」——供应链安全 fuzz 的常规操作 |
| 联系方式 (未留) | 机器人不留联系方式 |
| 时机 = 每次发布后 | PyPI 版本事件触发拉包 |

**无泄露风险**：env 只有 version/os（agent 场景加 model），不含用户数据，纯属骚扰。

### `_looks_like_probe` 保守启发式

```python
def _looks_like_probe(content: str, contact: str) -> bool:
```

判定顺序（**留了联系方式第一优先放行**）：

1. 留了联系方式 → 真人放行
2. content 空 → 探针
3. 含空格 → 真人放行（有语义句子）
4. 无空格含 `/` → 探针（纯路径；真人谈路径一般带说明文字）
5. 无路径无空格：`len < 4` 且不含中文字符 → 探针（ASCII 超短碎片是机器人特征）；中文短反馈（"很好用"）放行

**保守取向**：误伤代价 = 只是不推送，本地仍落盘（数据零丢失）。判定为探针时返回文案 `✅ 已记录（疑似自动探针，仅本地不推送）：...`。

### 验证与闭环

- 9 用例全过 + monkeypatch `_post_feishu` 端到端：探针 0 推送 / 真人正常推送
- **闭环的讽刺**：下次发版 → 扫描器拉新版探测 → 新版自带过滤把它拦了——发版触发的骚扰，被发出去的版本自己治好
- 作者侧无需清理：本机 `~/.agt/feedback/` 只有 7 月联调的 5 条，探针落盘都在扫描器自己的一次性沙箱里

## 注意事项

- `webhook_url` 随包默认指向作者飞书机器人——**这是探针能骚扰到作者的原因**；用户可在 `~/.agt/feedback.json` 覆盖或关 `enabled`
- 推送失败不抛错：返回文案带失败原因（HTTP code / 异常类型），数据已落盘
- 探针判定永不丢数据：只拦推送链路，落盘在前、判定在后

## 相关页面

- [工具外置判别标准](../architecture/tool-externalization-criteria.md)：feedback 被列为 A 类零引擎依赖（纯 HTTP 上报）的例子
- [运维与可观测性](../guides/ops.md)：存档布局与跨进程状态
