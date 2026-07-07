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

from session import Session, SESSIONS_DIR, list_sessions

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
        """处理一行输入。返回 True=是命令(已处理)，False=不是命令(交给 Agent)。"""
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
    """把 ['--k','v', 'pos'] 拆成 (位置参数, {flag: value/True})。"""
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
    print(f"✅ 会话已保存：{path.name}  (共 {len(ctx.session.turns)} 轮)")


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
    ctx.agent.session = new_session
    print(f"✅ 已恢复会话：{positional[0]}")
    print(new_session.summary_str())


def _cmd_list(ctx: CommandContext, args):
    files = list_sessions()
    if not files:
        print("📁 暂无保存的会话（用 /save <name> 保存当前会话）")
        return
    print(f"📁 已保存的会话（{len(files)} 个）：")
    print("-" * 64)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            turns = len(data.get("turns", []))
            first = data.get("turns", [{}])[0].get("user_message", "")[:30] if turns else "(空)"
            print(f"  {f.stem:<28} | {turns}轮 | 首轮：「{first}」")
        except Exception as e:
            print(f"  {f.name:<28} | 读取错误：{e}")
    print("-" * 64)


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
    ctx.agent.session = Session(
        system=ctx.agent.base_system, llm=ctx.agent.llm,
        recent_window_turns=ctx.agent.session.recent_window_turns)
    print("🔄 已重置会话（历史清空，system 保留）。")


def _to_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# 可配置项：名字 -> (设在 agent 还是 agent.llm 上, 类型转换)
CONFIGURABLE = {
    "max_steps": ("agent", int),
    "token_budget": ("agent", int),
    "max_retries": ("llm", int),
    "temperature": ("llm", float),
    "enable_thinking": ("llm", _to_bool),
}


def read_config(agent) -> dict:
    """读取所有可配置项的当前值。fallback_chain 以逗号分隔字符串展示。"""
    cfg = {k: getattr(agent if tgt == "agent" else agent.llm, k)
           for k, (tgt, _) in CONFIGURABLE.items()}
    chain = getattr(agent.llm, "fallback_chain", [])
    cfg["fallback_chain"] = ",".join(chain) if chain else ""
    return cfg


def apply_config(agent, values: dict) -> list:
    """应用一组配置（key->value），返回每项的结果文案列表。"""
    results = []
    # fallback_chain 特殊处理（逗号分隔 → list）
    if "fallback_chain" in values:
        v = values.pop("fallback_chain")
        chain = [m.strip() for m in str(v).split(",") if m.strip()]
        agent.llm.fallback_chain = chain
        results.append(f"✅ fallback_chain = {chain or '(空, 无回退)'}")
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
    from models import MODELS
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


def build_default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register("save", _cmd_save, "[name]  保存当前会话")
    reg.register("resume", _cmd_resume, "<name>  恢复指定会话")
    reg.register("list", _cmd_list, "列出所有已保存会话")
    reg.register("show", _cmd_show, "[name]  查看会话详情（不传=当前）")
    reg.register("reset", _cmd_reset, "重置会话（清空历史）")
    reg.register("config", _cmd_config, "<key> <value>  改运行时配置(max_steps/token_budget)")
    reg.register("budget", _cmd_budget, "查看本次 token 消耗")
    reg.register("model", _cmd_model, "[name]  列出/切换 LLM 模型")
    reg.register("reload_mcp", _cmd_reload_mcp, "<name>  重连指定 MCP server")
    # /help 需要访问 reg 自身，单独绑
    reg.register("help", lambda ctx, args: reg.print_help(), "显示本帮助")
    return reg
