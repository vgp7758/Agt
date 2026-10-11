# @file: 文件引用 · 内容型参数整值展开（src/agent.py `_expand_file_refs`，2026-10-11，用户提案，commit 557805a）

> 源码：`src/agent.py`（模块级 `_FILE_REF_PARAMS` 白名单 + `_expand_file_refs`，接线在 `Agent._exec_tool`）
> 职责：工具调用的内容型字符串参数支持 `@file:<path>` 整值引用——执行前自动读文件全文替换进参数。接线在统一入口，全部工具（内置/外置/MCP/插件）自动生效，无需逐工具改造。

## 职责与动机

代码块、message、prompt 这类**长文本参数**此前必须整段塞进 JSON tool call——转义痛、体积痛、迭代重发更痛（与 run_python inline 迭代浪费同一族问题，见 [run-python · inline vs file 经济学](run-python.md)）。用户提案：这类参数支持文件引用语法——**先 write_file 存内容，参数传 `@file:路径`**，执行时引擎自动读文件全文填入。

```python
# 定时任务推送长文案
add_schedule(name="晨报", every_seconds=3600, message="@file:prompts/morning.txt")

# 脚本落盘后引用重跑（改脚本只改文件，调用侧恒定）
run_python(code="@file:scripts/analyze_v2.py")
```

## 语法与语义（整值匹配，零歧义）

| 规则 | 行为 |
|---|---|
| **整值匹配** | 参数值 strip 后完全形如 `^@file:(.+)$` 才触发——文本**中间**出现 `@file:` 不误触，零歧义 |
| **路径基准** | 相对路径基于 workspace（`real_tools.WORKSPACE`），绝对路径亦可 |
| **成功** | `arguments[k]` 原地替换为文件全文（`read_text(utf-8, errors=replace)`）+ 附注 `📎 message = @file:xxx（已展开 1024 字）` |
| **失败** | **原值透传**（不炸调用）+ 附注 `⚠️ xxx = @file:yyy 读取失败（FileNotFoundError）——参数按原值传` |
| **白名单制** | 只认参数名——`path` / `name` 等路径/标识类参数**永不展开**（传路径就是路径） |

附注 prepend 到工具结果头部——模型当步可见（与[越界放行提示](tool-approval.md)同一「工具结果通道」哲学）。

## 白名单：八个内容型参数（`_FILE_REF_PARAMS`）

`code` · `message` · `content` · `prompt` · `text` · `new_string` · `old_string` · `system`

按**参数名**全局匹配而非按工具：任何工具只要有同名参数就享受展开（如所有工具的 `message`）。这是特性也是边界——若未来某工具的同名参数语义不是「内容」需再评估（当前工具面无冲突）。

## 接线点：`_exec_tool` 统一入口——远程路由【之前】

`Agent._exec_tool`（src/agent.py）在解析完 remote_instance_id / 粘性路由参数之后、真正执行之前：

```python
_fr = _expand_file_refs(name, arguments)          # 就地改写 arguments，返回附注清单
_fr_prefix = ("\n".join(_fr) + "\n\n") if _fr else ""
if rid:
    r = route_remote_call(rid, name, arguments)   # 远端拿到的是【展开后】的全文
    ...
    return (_fr_prefix + r) if _fr_prefix else r
r = self.tools.call(name, arguments)              # 本地路径同样 prepend 附注
```

**在远程路由之前展开是有意的**：引用的是**本地**上下文的文件（模型 read_file/write_file 操作过的）——远端实例拿到的就是完整文本，无需远端也存在该文件（组网语义见 [多实例组网](../architecture/multi-instance.md)）。本地/远端两条路径的结果都带 `📎` 头。

## 可发现性：四处 param_descriptions

模型怎么知道有这语法？高频长文本参数的描述里已注明（schema 驱动行为，与 [run-python 决策指引](run-python.md)同款手法）：

| 工具 | 参数 | 所在文件 |
|---|---|---|
| run_python | `code` | src/real_tools.py |
| write_file | `content`（「从模板文件复制内容时用」） | src/real_tools.py |
| edit | `new_string` | src/real_tools.py |
| add_schedule | `message` | src/background_tools.py |

🔧 [工具表单](tool-form.md)两级弹窗的 placeholder 读参数 description——人手动调用时同样可见。

## 验证（六场景 ALL PASS，2026-10-11 开发轮）

```
① 相对路径展开（code ← 文件全文 + 📎 附注）✓     ④ path 参数不展开（白名单外）✓
② 绝对路径展开 ✓                                ⑤ 非整值（文本中间出现 @file:）不触发 ✓
③ 不存在文件 → 原值透传 + ⚠️ 附注 ✓              ⑥ _exec_tool 集成：tools.call 收到展开后全文、返回带 📎 头 ✓
```

**发布**：随 v0.34.5 发布（2026-10-11，release commit `a2cddd1`）——与 call_again（[toollog](toollog.md)）、trace 内部滚动（[trace-fold](trace-fold.md)）同批进版。

## 施工插曲

模块级函数首版插错定点——落进了 `Agent` 类体中间，把类拦腰截断（后续所有方法脱离类作用域）。当轮发现后整段摘出、验证类恢复完整，再重新落位到 `class Agent:` 之前的模块级位置（现行 L227-263）。教训：往大文件插模块级代码，定点锚要用**类定义边界**而不是方法行号。

## 注意事项

- 生效需 `/restart`
- 读文件固定 `utf-8, errors=replace`——GBK 文件不会炸但中文变 U+FFFD（读取侧暂无 utf-8→gbk 探测；[run_python 输出侧已有](run-python.md)，可对齐）
- 大文件无上限——引用了超大文件就全量进参数/投影，使用者自行把握（与 recent-file 快照的 100K 上限不同，此处不截断）
- `old_string` 也在白名单：edit 的 old_string 整值 `@file:` 引用即「用文件内容做匹配锚」——少见的合法用法，语义成立

## 相关页面

- [工具执行审批](tool-approval.md) — 同在 `_exec_tool` 调用管线上的预处理/护栏两兄弟；「工具结果通道」提示同哲学
- [run_python](run-python.md) — `code` 参数引用落盘脚本；inline 迭代重发浪费的又一缓解
- [background-scheduler](background-scheduler.md) — `add_schedule.message` 定时推送长文案的引用入口
- [多实例组网](../architecture/multi-instance.md) — 展开在远程路由前：远端拿到全文
- [工具表单](tool-form.md) — description → placeholder，引用语法对人也可见
