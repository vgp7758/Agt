# 生成图自动可视 · step 生成图片自动挂投影（伪造 read_file 对，2026-10-07，用户提案，commit 218a757）

> src/agent.py（after_tool changed_files 收集点，L2474 附近）+ src/session.py（`collect_generated_images` 暂存 L3684 + 投影尾部伪造对注入 L2208）。commit `218a757` 已推送（site-packages 已同步），`/restart` 生效。

## 动机与形态（用户提案）

对有视觉能力的模型 agent：step 前后工具生成了图片（落盘进 changed_files），本轮投影自动把图带上——**不需要入 event.jsonl，也省一步真实的 read_file 读图**。

实现取「伪造 read_file 对」形态：投影尾部附加一对消息——assistant 的 `tool_calls: read_file(path)` + tool 结果（text 块 + `image_url` 块挂同一条 tool 消息）。视觉模型**"以为"自己调用过 read_file 看图**：叙事与其工具调用历史连贯（这文件确实是它生成的），实际是框架自动注入。

投影形态：

```json
{"role": "assistant", "content": null,
 "tool_calls": [{"id": "genimg_da1c4e9", "type": "function",
                 "function": {"name": "read_file", "arguments": "{\"path\": \"chart.png\"}"}}]}
{"role": "tool", "tool_call_id": "genimg_da1c4e9",
 "content": [{"type": "text", "text": "✅ 已读取 chart.png（520KB 图片，像素级内容见下图）"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]}
```

## 收集链：changed_files → 白名单 → 去重暂存（`collect_generated_images`）

| 环节 | 实现 |
|---|---|
| 挂点 | src/agent.py 单步工具调用后的 changed_files 处理处 → `session.collect_generated_images(changed)`（try/except 兜底——收集失败不影响工具本身），紧邻 after_tool 钩子派发 |
| 路径解析 | 绝对路径原样；workspace 相对路径补全（`self.workspace / rel`） |
| 扩展白名单 | `_GEN_IMG_EXTS = {.png, .jpg, .jpeg, .webp, .gif}`——非图片扩展直接跳过 |
| 去重 | `Session._gen_images_done` 记账（key = 绝对路径 lower）——同文件只注入一次 |
| 尺寸上限 | 单图 ≤1.5MB（1_572_864 字节），超限跳过；一次最多暂存 **4 张** |
| 暂存 | `Session._gen_images_pending`：`[{path, call_id, data(dataURL), kb}]` |
| call_id | `genimg_<md5(路径)[:10]>`——稳定幂等，重投影不换 id（缓存友好） |
| mime | 后缀映射（png / jpg·jpeg / webp / gif），未知兜底 image/png |

**生效时机**：收集发生在工具调用步内，本轮**下一次投影**即带上（agent.py 注释：视觉模型下一步直接看到像素）。

## 装配注入：投影尾部（image_feed 通道之后，src/session.py）

- **vision 门控**：`getattr(getattr(self, "llm", None), "vision_supported", False)` 才注入——非视觉模型投影零变化（挂图会被端点拒收）
- **瞬态语义**：组装层注入，不落 events.jsonl / step 存档——读档回看与 WebUI 历史不含伪造对，历史叙事以真实 event 为准
- **可观测**：投影段统计有对应计段 `gen_images(生成图自动可视)`（sample=`[N 张生成图]`，备注「伪造read_file对·瞬态不落档」，msgs_n=0 不计消息数），/context 段落表可见
- 门控键小插曲：初版写 `getattr(self.llm, "vision", False)`，当场纠正为 **`vision_supported`**（与 [image_feed](image-feed.md) 求值同键；该键是视觉能力位真源，见 [LLM 客户端韧性](llm-network-resilience.md)），并加双层 getattr 防 self.llm 缺失

## 验证（冒烟四场景全绿，2026-10-07）

- png 产物收录 ✓（伪造对形态正确：tool_calls + text/image_url 双块）
- txt 产物跳过 ✓（白名单外）
- 2MB 大图跳过 ✓（超 1.5MB 单图上限）
- 同文件重复产出去重 ✓（`_gen_images_done` 记账）

## 与其它图片通道的分工

| 通道 | 方向 | 载体 | 持久性 |
|---|---|---|---|
| [图片输入](image-input.md) | 用户 → 模型 | `Turn.images` 原生多模态 / 插话 `<img>` 标签按 vision 展开 | Turn 存档 |
| `<img>` 标签指路 | 发标签方 → 模型 | ` [图片 name 读取失败] ` 标签 + 图须落 `repo_images_dir()` 契约位置（job-hunt 实战翻车即栽在落盘位置） | 随消息文本存档 |
| [image_feed](image-feed.md) | 画面服务 → 模型 | 每步最新帧挂投影尾部（tail_images 通道） | 瞬态 |
| **本特性** | 工具产物 → 模型 | changed_files 新增图伪造 read_file 对——**工具零配合**（不用发标签、图落 workspace 任意位置） | 瞬态 |

## 注意事项

- **暂存队列无清空逻辑**（当前形态）：`_gen_images_pending` 随 session 生命周期持续注入，封顶 4 张后收满不再收新图——同一会话后续每步投影都带这几张。若后续要"只跟本轮"语义，可参照 [recent-file](../architecture/context-engine.md) 的轮尾去重口径迭代
- 图是 base64 data URL 直进投影：受单图 1.5MB / 一次 4 张双上限保护，token 统计不计图（msgs_n=0）
- 去重粒度是**路径**：同名路径重新生成的更新版不会重新注入（`_gen_images_done` 已记账）

## 相关页面

- [图片输入链路](image-input.md) —— vision 门控与 image_url 块基建（`_norm_img_data_url` / 拒图自愈 / `<img>` 标签）
- [image_feed](image-feed.md) —— 同投影尾部家族：每步最新帧
- [LLM 客户端韧性](llm-network-resilience.md) —— `vision_supported` 能力位与端点拒图自愈
