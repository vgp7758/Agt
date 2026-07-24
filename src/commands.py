"""commands.py —— REPL 内的斜杠命令（Step 8 强化，对应需求#1）。

支持 /name --flag value 位置参数 形式。命令分两类：
  - 会话/控制：/save /resume /list /show /reset /config /budget /help
  - 便捷快捷：/tank（打印当前坦克段位）
AgenTank 的具体操作（模拟/发布/挑战）仍作为工具由 Agent 自主调用，不做成命令。
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from session import Session, SESSIONS_DIR, list_sessions, session_meta

if TYPE_CHECKING:
    from agent import Agent


@dataclass
class CommandContext:
    agent: "Agent"  # 提供 session / llm / base_system / max_steps / token_budget / cumulative_tokens

    @property
    def session(self) -> Session:
        return self.agent.session


class CommandRegistry:
    def __init__(self):
        self._cmds: dict[str, tuple[Callable, str]] = {}

    def register(self, name: str, handler: Callable, help_text: str = ""):
        self._cmds[name] = (handler, help_text)

    def dispatch(self, line: str, ctx: CommandContext) -> bool:
        """处理一行输入。返回 True=是命令 (已处理)，False=不是命令 (交给 Agent)。"""
        if not line.startswith("/"):
            return False
        try:
            parts = shlex.split(line[1:])
        except ValueError:
            parts = line[1:].split()
        if not parts:
            return False
        name, args = parts[0], parts[1:]
        if name not in self._cmds:
            print(f"❌ 未知命令 /{name}，输入 /help 查看可用命令")
            return True
        self._cmds[name][0](ctx, args)
        return True

    def print_help(self):
        print("\n可用命令：")
        for name, (_, help_text) in self._cmds.items():
            print(f"  /{name:<8} {help_text}")
        print()


# ========== 参数解析 ==========

def _parse_args(args: list[str]) -> tuple[list[str], dict]:
    """把 ['--k','v', 'pos'] 拆成 (位置参数，{flag: value/True})。"""
    positional, flags = [], {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                flags[key] = args[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    return positional, flags


# ========== 命令实现 ==========

def _cmd_save(ctx: CommandContext, args):
    name = _parse_args(args)[0][0] if args else None
    path = ctx.session.save(name)
    note = "（日常已自动落盘，本次手动改名另存）" if name else "（日常已自动落盘，本次强制再存一次）"
    print(f"✅ 已保存：{path.name}  (共 {len(ctx.session.turns)} 轮) {note}")


def _cmd_resume(ctx: CommandContext, args):
    positional = _parse_args(args)[0]
    if not positional:
        print("用法：/resume <name>  （先用 /list 查看可用会话）")
        return
    try:
        new_session = Session.load(positional[0], llm=ctx.agent.llm)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    ctx.agent.set_session(new_session)
    print(f"✅ 已恢复会话：{positional[0]}")
    print(new_session.summary_str())


def _cmd_list(ctx: CommandContext, args):
    files = list_sessions()
    if not files:
        print("📁 暂无保存的会话（每轮会自动落盘；/save <name> 可改名另存）")
        return
    print(f"📁 已保存的会话（{len(files)} 个，按最近修改倒序）：")
    print("-" * 72)
    for f in files:
        meta = session_meta(f)
        print(f"  {meta['name'][:26]:<26} | {meta['turns']:>3}轮 | /resume {meta['id']}")
        if meta["first"] and meta["first"] != "(读取失败)":
            print(f"  {'':<26} | 首轮：「{meta['first']}」")
    print("-" * 72)


def _cmd_recall(ctx: CommandContext, args):
    positional = _parse_args(args)[0]
    if not positional:
        print("用法：/recall <关键词>  在全部历史轮次里搜索，召回匹配轮的完整内容（不含思考过程）")
        return
    print(ctx.session.recall(" ".join(positional)))


def _cmd_show(ctx: CommandContext, args):
    positional = _parse_args(args)[0]
    if not positional:
        print(ctx.session.summary_str())
        return
    try:
        s = Session.load(positional[0], llm=ctx.agent.llm)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    print(s.summary_str())


def _cmd_reset(ctx: CommandContext, args):
    from session import Session  # 局部 import 避免循环
    from plan_tools import clear_active_plan
    ctx.agent.set_session(Session(
        system=ctx.agent.base_system, llm=ctx.agent.llm,
        recent_window_turns=ctx.agent.session.recent_window_turns))
    clear_active_plan(ctx.agent)       # 重置：连计划（id/active_plan 一并清）、自主模式一起清空
    ctx.agent.exit_autonomous_mode()
    ctx.agent.goal_check_script = ""
    print("🔄 已重置会话（历史、计划、自主模式均清空，system 保留）。")


def _to_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _policy_cast(v):
    """fallback_policy 取值校验：只接受 sticky / reset。非法值抛异常由 apply_config 兜底报错。"""
    s = str(v).strip().lower()
    if s not in ("sticky", "reset"):
        raise ValueError(f"fallback_policy 只能是 sticky/reset，收到 {v}")
    return s


def _str_or_none(v):
    """非空字符串原样（去空白），空串→None。用于 reasoning_completer 等可空配置。"""
    s = str(v).strip()
    return s or None


# 可配置项：名字 -> (设在 agent 还是 agent.llm 上，类型转换)
CONFIGURABLE = {
    "max_steps": ("agent", int),
    "token_budget": ("agent", int),
    "max_retries": ("llm", int),
    "temperature": ("llm", float),
    "enable_thinking": ("llm", _to_bool),
    "fallback_policy": ("llm", _policy_cast),
    "reasoning_completer": ("llm", _str_or_none),
}


def read_config(agent) -> dict:
    """读取所有可配置项的当前值。fallback_chain 以逗号分隔字符串展示。"""
    cfg = {k: getattr(agent if tgt == "agent" else agent.llm, k)
           for k, (tgt, _) in CONFIGURABLE.items()}
    chain = getattr(agent.llm, "fallback_chain", [])
    cfg["fallback_chain"] = ",".join(chain) if chain else ""
    try:
        import real_tools
        cfg["tool_timeout"] = real_tools.TOOL_TIMEOUT
    except Exception:
        cfg["tool_timeout"] = 10
    return cfg


def apply_config(agent, values: dict) -> list:
    """应用一组配置（key->value），返回每项的结果文案列表。"""
    results = []
    # tool_timeout 特殊处理（real_tools 全局，不在 agent/llm）
    if "tool_timeout" in values:
        v = values.pop("tool_timeout")
        try:
            import real_tools
            results.append(real_tools.set_tool_timeout(int(v)))
        except Exception as e:
            results.append(f"❌ tool_timeout 值非法：{v}（{e}）")
    # fallback_chain 特殊处理（逗号分隔 → list）
    if "fallback_chain" in values:
        v = values.pop("fallback_chain")
        chain = [m.strip() for m in str(v).split(",") if m.strip()]
        agent.llm.fallback_chain = chain
        results.append(f"✅ fallback_chain = {chain or '(空，无回退)'}")
    for k, v in values.items():
        if k not in CONFIGURABLE:
            results.append(f"❌ 未知配置 {k}（可配置：{list(CONFIGURABLE)}）")
            continue
        tgt, cast = CONFIGURABLE[k]
        try:
            cv = cast(v)
        except Exception:
            results.append(f"❌ {k} 值非法：{v}")
            continue
        setattr(agent if tgt == "agent" else agent.llm, k, cv)
        results.append(f"✅ {k} = {cv}")
    return results


def _cmd_config(ctx: CommandContext, args):
    positional = _parse_args(args)[0]
    if not positional:
        print("当前配置：")
        for k, v in read_config(ctx.agent).items():
            print(f"  {k} = {v}")
        print("用法：/config <key> <value> [<key> <value> ...]；可配置：" + " / ".join(CONFIGURABLE))
        return
    if len(positional) % 2 != 0:
        print("❌ 参数须成对，如：/config max_steps 100 token_budget 100000")
        return
    values = {positional[i]: positional[i + 1] for i in range(0, len(positional), 2)}
    for line in apply_config(ctx.agent, values):
        print(line)


def _cmd_budget(ctx: CommandContext, args):
    used = ctx.agent.cumulative_tokens
    budget = ctx.agent.token_budget
    pct = (used / budget * 100) if budget else 0
    print(f"💰 本次运行 token：已用 {used} / 预算 {budget} ({pct:.0f}%)")


def _cmd_reload_mcp(ctx: CommandContext, args):
    """断开并重连指定 MCP server，使代码修改后生效。"""
    positional = _parse_args(args)[0]
    if not positional:
        print("用法：/reload_mcp <name>  （.mcp.json 中 mcpServers 的键名）")
        return
    name = positional[0]
    tool = next((t for t in ctx.agent.tools if t.name == "reload_mcp_server"), None)
    if tool is None:
        print("❌ reload_mcp_server 工具未注册（MCP 未启用）")
        return
    print(tool.run(name=name))


def _cmd_model(ctx: CommandContext, args):
    import config
    MODELS = config.MODELS   # 用 config.MODELS（含 WebUI 热更新的用户模型），而非静态 models.py 兜底文件
    positional = _parse_args(args)[0]
    if not positional:
        print("可用模型（← 当前）:")
        for name, m in MODELS.items():
            cur = "  ← 当前" if name == ctx.agent.model_name else ""
            print(f"  {name}{cur}: {m.get('desc', '')}  [{m['model']}]")
        return
    name = positional[0]
    if name not in MODELS:
        print(f"❌ 未知模型 {name}，可用：{list(MODELS)}")
        return
    ctx.agent.switch_model(name)
    m = MODELS[name]
    print(f"✅ 已切换到 {name}: {m['model']} @ {m['base_url']}")


def _cmd_autonomous(ctx: CommandContext, args):
    """纯自主模式控制：/autonomous on <时间> /autonomous off /autonomous status"""
    from datetime import datetime, timedelta

    if not args:
        # 显示状态
        if not ctx.agent.autonomous_mode:
            print("纯自主模式：未开启")
        elif ctx.agent.is_autonomous_active():
            print(f"纯自主模式：已开启，持续到 {ctx.agent.autonomous_end_time.strftime('%Y-%m-%d %H:%M')}")
            print(f"自动提示词：{ctx.agent.autonomous_prompt}")
            print(f"待处理消息队列：{len(ctx.agent.pending_messages)} 条")
        else:
            print("纯自主模式：已超时（自动关闭）")
        print("\n用法：")
        print("  /autonomous on 17:30           # 到今天 17:30")
        print("  /autonomous on 2026-07-09 10:00  # 到指定时间")
        print("  /autonomous duration 30        # 持续 30 分钟")
        print("  /autonomous off                # 手动关闭")
        print("  /autonomous status             # 查看状态")
        print("  /autonomous prompt <文字>       # 修改自动继续提示词")
        print("  /autonomous goal <Python脚本>    # 设目标验证脚本(PASS=达成)")
        print("  /autonomous check               # 手动运行目标验证脚本")
        return

    cmd = args[0].lower()
    if cmd in ("on", "start"):
        if len(args) < 2:
            print("❌ 请指定结束时间，如：/autonomous on 17:30")
            return
        time_str = args[1]
        try:
            # 尝试解析 "YYYY-MM-DD HH:MM"
            try:
                target = datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            except ValueError:
                # 尝试解析 "HH:MM"（今天）
                today = datetime.now().date()
                target = datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %H:%M")
                if target < datetime.now():
                    target += timedelta(days=1)
            ctx.agent.set_autonomous_mode(target)
            print(f"✅ 纯自主模式已开启，持续到 {target.strftime('%Y-%m-%d %H:%M')}")
        except Exception as e:
            print(f"❌ 开启失败：{type(e).__name__}: {e}")

    elif cmd in ("off", "stop", "exit"):
        ctx.agent.exit_autonomous_mode()
        print("✅ 纯自主模式已关闭")

    elif cmd == "duration":
        if len(args) < 2:
            print("❌ 请指定持续分钟数，如：/autonomous duration 30")
            return
        try:
            minutes = int(args[1])
            target = datetime.now() + timedelta(minutes=minutes)
            ctx.agent.set_autonomous_mode(target)
            print(f"✅ 纯自主模式已开启，持续 {minutes} 分钟（到 {target.strftime('%Y-%m-%d %H:%M')}）")
        except Exception as e:
            print(f"❌ 开启失败：{type(e).__name__}: {e}")

    elif cmd == "status":
        if not ctx.agent.autonomous_mode:
            print("纯自主模式：未开启")
        elif ctx.agent.is_autonomous_active():
            print(f"纯自主模式：已开启，持续到 {ctx.agent.autonomous_end_time.strftime('%Y-%m-%d %H:%M')}")
            print(f"自动提示词：{ctx.agent.autonomous_prompt}")
            print(f"待处理消息队列：{len(ctx.agent.pending_messages)} 条")
        else:
            print("纯自主模式：已超时（自动关闭）")

    elif cmd == "prompt":
        if len(args) < 2:
            print(f"当前自动提示词：{ctx.agent.autonomous_prompt}")
            print("用法：/autonomous prompt <新的提示词>")
            return
        new_prompt = " ".join(args[1:])
        ctx.agent.autonomous_prompt = new_prompt
        print(f"✅ 自动提示词已更新：{new_prompt}")

    elif cmd == "goal":
        print("用法：/autonomous goal <Python脚本内容>")
        print("  脚本须在目标达成时 print('PASS')，否则输出当前状态（如分数）")
        print("  自主循环每轮结束后自动跑该脚本检查")
        print("示例：")
        print('  /autonomous goal "print(\\"PASS\\") if score >= 3000 else print(score)"')
        return

    elif cmd == "check":
        if not ctx.agent.goal_check_script:
            print("(未设置目标验证脚本，用 /autonomous goal 设置)")
            return
        print("🔍 运行目标验证脚本…")
        result = ctx.agent.run_goal_check()
        print(f"结果：{result}")

    else:
        print(f"❌ 未知子命令：{cmd}，输入 /autonomous 查看用法")


def _cmd_workflows(ctx: CommandContext, args):
    """工作流管理：/workflows（列出） / /workflows reload（重载）"""
    from real_tools import WORKSPACE
    from workflow import workflows_info, refresh_workflow_tools

    sub = args[0].lower() if args else "list"
    if sub == "reload":
        ok, broken = refresh_workflow_tools(ctx.agent.tools, WORKSPACE, ctx.agent)
        print(f"🔄 已重载：{len(ok)} 个可用" + (f"，{len(broken)} 个失败" if broken else ""))
        for name, err in broken:
            print(f"  ⚠️ {name}: {err}")
        return

    items = workflows_info(WORKSPACE)
    if not items:
        print("📁 .agent/workflows/ 为空或不存在（放 Coze 画布 .json + .json.meta 即可）")
        return
    mark = {"ok": "✅", "warn": "⚠️", "error": "❌", "disabled": "⏸"}
    print(f"🧩 工作流（{len(items)} 个）：")
    for it in items:
        desc = f"（{it['description']}）" if it["description"] else ""
        print(f"  {mark.get(it['status'], '?')} {it['tool']}{desc}")
        if it["detail"]:
            print(f"      └ {it['detail']}")
    print("用法：/workflows reload  重新扫描注册")


def _cmd_memory(ctx: CommandContext, args):
    """/memory 长期记忆管理（跨 session，~/.agt/repos/<hash>/memories/）。
    子命令：overview(默认) / list / show / add / delete / search / semantic"""
    from longterm_memory import TYPES
    ltm = ctx.agent.ltm
    positional, flags = _parse_args(args)
    sub = positional[0].lower() if positional else "overview"

    if sub == "overview":
        print(ltm.overview())
        print("\n用法：/memory list [--type T] [--query Q] | show <id> | "
              "add --type T --title .. --content .. [--tags a,b] | delete <id> | search <词> | semantic")

    elif sub == "list":
        t = flags.get("type") or None
        q = flags.get("query") or None
        if isinstance(t, bool):
            t = None
        if isinstance(q, bool):
            q = None
        if t and t not in TYPES:
            print(f"❌ --type 只能是 {list(TYPES)}")
            return
        items = ltm.list(type_=t, query=q)
        if not items:
            print("(空；用 /memory add 记一笔，或让 Agent 自主 add_memory)")
            return
        for r in items:
            preview = r["content"][:60] + ("…" if len(r["content"]) > 60 else "")
            print(f"  [{r['id']}]({r['type']}) {r['title']}：{preview}")

    elif sub == "show":
        if len(positional) < 2:
            print("用法：/memory show <id>")
            return
        rec = ltm.get(positional[1])
        if not rec:
            print(f"❌ 找不到 {positional[1]}")
            return
        print(json.dumps(rec, ensure_ascii=False, indent=2))

    elif sub == "add":
        t, title, content = flags.get("type"), flags.get("title"), flags.get("content")
        # _parse_args 对裸 --flag 返回 True；缺值/未传都视为非法
        if not t or isinstance(t, bool) or not title or isinstance(title, bool) \
                or not content or isinstance(content, bool):
            print("用法：/memory add --type <semantic|episodic|procedural> --title <标题> --content <内容> [--tags a,b]")
            print('  多词参数请用引号包裹，如 --title "用户背景" --content "Unity 背景，转型 AI"')
            return
        if t not in TYPES:
            print(f"❌ --type 只能是 {list(TYPES)}")
            return
        tags_val = flags.get("tags", "")
        if isinstance(tags_val, bool):
            tags_val = ""
        tag_list = [x.strip() for x in str(tags_val).split(",") if x.strip()]
        res = ltm.add(t, title, content, tag_list, origin_session=ctx.session.name)
        verb = "更新" if res["action"] == "updated" else "记录"
        print(f"✅ 已{verb} [{res['id']}]「{title}」")

    elif sub == "delete":
        if len(positional) < 2:
            print("用法：/memory delete <id>")
            return
        ok = ltm.delete(positional[1])
        print(f"🗑️ 已删除 {positional[1]}" if ok else f"❌ 找不到 {positional[1]}")

    elif sub == "search":
        rest = positional[1:]
        if not rest:
            print("用法：/memory search <关键词>")
            return
        hits = ltm.search(" ".join(rest), limit=15)
        if not hits:
            print("(无匹配)")
            return
        print(f"找到 {len(hits)} 条：")
        for r in hits:
            preview = r["content"][:60] + ("…" if len(r["content"]) > 60 else "")
            print(f"  [{r['id']}]({r['type']}) {r['title']}：{preview}")

    elif sub == "semantic":
        block = ltm.static_block()
        print(block or "(semantic 与 procedural 均为空，暂无始终注入内容)")

    else:
        print(f"❌ 未知子命令 {sub}；可用：list / show / add / delete / search / semantic")


def _cmd_logs(ctx: CommandContext, args):
    """/logs [N]  打印当前 session 日志文件（<name>.log）的尾部 N 行，默认 30。"""
    from log import session_log_path
    n = 30
    if args and args[0].isdigit():
        n = int(args[0])
    name = ctx.session.name
    if not name:
        print("(当前 session 还没命名，首轮完成后才生成 <name>.log)")
        return
    p = session_log_path(ctx.session.workspace, name)
    if not p.exists():
        print(f"(日志文件不存在：{p.name}；本轮可能还在内存缓冲，首轮完成后落盘)")
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    tail = lines[-n:] if len(lines) > n else lines
    print(f"📜 {p.name}（共 {len(lines)} 行，显示尾 {len(tail)} 行）：")
    for line in tail:
        print(line)


def _cmd_download(ctx: CommandContext, args):
    """/download [list|<name> [dir] [--force]]  下载随包资产（工作流/mcp/脚本）。"""
    from download import list_assets, download_asset
    positional, flags = _parse_args(args)
    force = bool(flags.get("force"))
    if not positional or positional[0] == "list":
        items = list_assets(workspace=ctx.session.workspace)
        if not items:
            print("(无随包资产)")
            return
        print(f"📦 随包资产（{len(items)} 项）：")
        for a in items:
            mark = "✅已在本机" if a.get("exists") else "⬇可下载"
            print(f"  [{mark}] {a['name']} ({a['type']}) — {a['desc']}")
        print("用法：/download <name> [dir] [--force]  （name 来自上面清单）")
        return
    name = positional[0]
    target = positional[1] if len(positional) > 1 else None
    print(download_asset(name, target_dir=target, force=force, workspace=ctx.session.workspace))


def _cmd_feedback(ctx: CommandContext, args):
    """/feedback [类型] <内容>  提交反馈给作者（bug/建议/问题/赞美），类型可选默认「建议」。
    不带参数显示用法 + 作者联系方式。反馈本地保存，作者配了 webhook 则同时推送到飞书。"""
    from feedback import submit_feedback, VALID_KINDS, author_contact_str
    if not args:
        print("用法：/feedback [类型] <反馈内容>")
        print("  类型可选（默认「建议」）：" + " / ".join(VALID_KINDS))
        print('  示例：/feedback bug 工作流调试页白屏')
        print('        /feedback 希望支持 Mermaid 图渲染')
        contact = author_contact_str()
        if contact:
            print(f"  直接联系作者：{contact}")
        return
    # 首词若是合法类型，吃掉作类型；否则整体当内容、类型默认「建议」
    if args[0] in VALID_KINDS:
        kind, content = args[0], " ".join(args[1:])
    else:
        kind, content = "建议", " ".join(args)
    print(submit_feedback(kind, content, agent=ctx.agent))


def build_default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register("save", _cmd_save, "[name]  保存当前会话")
    reg.register("resume", _cmd_resume, "<name>  恢复指定会话")
    reg.register("list", _cmd_list, "列出所有已保存会话")
    reg.register("show", _cmd_show, "[name]  查看会话详情（不传=当前）")
    reg.register("recall", _cmd_recall, "<关键词>  召回包含该词的历史轮次完整内容")
    reg.register("reset", _cmd_reset, "重置会话（清空历史）")
    reg.register("config", _cmd_config, "<key> <value>  改运行时配置 (max_steps/token_budget)")
    reg.register("budget", _cmd_budget, "查看本次 token 消耗")
    reg.register("model", _cmd_model, "[name]  列出/切换 LLM 模型")
    reg.register("reload_mcp", _cmd_reload_mcp, "<name>  重连指定 MCP server")
    reg.register("autonomous", _cmd_autonomous, "纯自主模式控制 (on/off/status/duration/prompt)")
    reg.register("workflows", _cmd_workflows, "[reload]  列出/重载 .agent/workflows/ 工作流")
    reg.register("memory", _cmd_memory, "[list|show|add|delete|search|semantic]  长期记忆管理")
    reg.register("logs", _cmd_logs, "[N]  查看当前 session 日志尾部（默认30行）")
    reg.register("download", _cmd_download, "[name|list] [dir] [--force]  下载随包资产（工作流/mcp/脚本）")
    reg.register("feedback", _cmd_feedback, "[类型] <内容>  提交反馈给作者（bug/建议/问题/赞美）")
    # /help 需要访问 reg 自身，单独绑
    reg.register("help", lambda ctx, args: reg.print_help(), "显示本帮助")
    return reg
