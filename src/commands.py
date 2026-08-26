"""commands.py —— REPL 内的斜杠命令（Step 8 强化，对应需求#1）。

支持 /name --flag value 位置参数 形式。命令分两类：
  - 会话/控制：/save /resume /list /show /reset /config /budget /help
  - 便捷快捷：/tank（打印当前坦克段位）
AgenTank 的具体操作（模拟/发布/挑战）仍作为工具由 Agent 自主调用，不做成命令。
"""
from __future__ import annotations

import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from session import Session, SESSIONS_DIR, list_sessions

if TYPE_CHECKING:
    from agent import Agent


@dataclass
class CommandContext:
    agent: "Agent"  # 提供 session / llm / base_system / max_steps / token_budget / cumulative_tokens
    work_q: object = None  # chat 主循环的 work_q（/web 启动服务时注入，让 WS 文本消息回流主循环）
    state: object = None  # chat 主循环的 state dict（含 busy）；/web 注入给 server，供 WS 文本按忙/闲路由

    @property
    def session(self) -> Session:
        return self.agent.session


class CommandRegistry:
    def __init__(self):
        self._cmds: dict[str, tuple[Callable, str, str]] = {}

    def register(self, name: str, handler: Callable, help_text: str = "", detail: str = ""):
        self._cmds[name] = (handler, help_text, detail)

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
        name = parts[0]
        if name not in self._cmds:
            print(f"❌ 未知命令 /{name}，输入 /help 查看可用命令")
            return True
        # /call 参数含 JSON（双引号/括号），shlex 会破坏引号 → 传原始字符串
        if name == "call":
            rest = line[1:].split(None, 1)
            args = rest[1] if len(rest) > 1 else ""
        else:
            args = parts[1:]
        self._cmds[name][0](ctx, args)
        return True

    def print_help(self):
        """打印详细帮助：每条命令一行摘要 + 多行详细用法（有 detail 的展开）。"""
        print("\n" + "=" * 64)
        print("📋 可用命令（详细用法）")
        print("=" * 64)
        for name, (_, help_text, detail) in self._cmds.items():
            print(f"\n  /{name}")
            if help_text:
                print(f"    {help_text}")
            if detail:
                for line in detail.strip().split("\n"):
                    print(f"    {line}")
        print("\n" + "=" * 64)
        print("💡 提示：直接输入自然语言即可与 Agent 对话；斜杠命令用于会话管理/调试/配置。")
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
    name = " ".join(_parse_args(args)[0]).strip() or None
    ctx.session.save(name)
    note = "（日常已自动落盘，本次手动改名另存）" if name else "（日常已自动落盘，本次强制再存一次）"
    print(f"✅ 已保存：{ctx.session.name}  (共 {len(ctx.session.turns)} 轮) {note}")


def _cmd_rename(ctx: CommandContext, args):
    new = " ".join(_parse_args(args)[0]).strip()
    if not new:
        print("用法：/rename <新会话名>（可含空格，如 /rename 我的 项目 笔记）")
        return
    try:
        old = ctx.session.name or "(未命名)"
        ctx.session.rename(new)
        print(f"✅ 会话已重命名：{old} → {ctx.session.name}")
        # 通知 WebUI 同步标题（CLI 无 on_event 时跳过）
        if getattr(ctx.agent, "on_event", None):
            try:
                ctx.agent.on_event({"type": "session_renamed", "name": ctx.session.name})
            except Exception:
                pass
    except ValueError as e:
        print(f"❌ {e}")


def _cmd_resume(ctx: CommandContext, args):
    name = " ".join(_parse_args(args)[0]).strip()
    if not name:
        print("用法：/resume <name>  （先用 /list 查看可用会话；名称可含空格）")
        return
    try:
        new_session = Session.load(name, llm=ctx.agent.llm, workspace=ctx.session.workspace)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return
    ctx.agent.set_session(new_session)
    print(f"✅ 已恢复会话：{name}")
    print(new_session.summary_str())


def _cmd_list(ctx: CommandContext, args):
    sessions = list_sessions()
    if not sessions:
        print("📁 暂无保存的会话（每轮会自动落盘；/save <name> 可改名另存）")
        return
    print(f"📁 已保存的会话（{len(sessions)} 个，按创建时间倒序）：")
    print("-" * 72)
    for s in sessions:
        name_display = s.get("name") or s.get("id")
        turns = s.get("turns", 0)
        sid = s.get("id")
        first = s.get("first")
        created = s.get("created_at")
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(created)) if created else ""
        print(f"  {name_display[:26]:<26} | {ts_str:<12} | {turns:>3}轮 | /resume {sid}")
        if first:
            print(f"  {'':<26} | 首轮：「{first}」")
    print("-" * 72)


def _cmd_recall(ctx: CommandContext, args):
    positional = _parse_args(args)[0]
    if not positional:
        print("用法：/recall <关键词>  在全部历史轮次里搜索，召回匹配轮的完整内容（不含思考过程）")
        return
    print(ctx.session.recall(" ".join(positional)))


def _cmd_show(ctx: CommandContext, args):
    name = " ".join(_parse_args(args)[0]).strip()
    if not name:
        print(ctx.session.summary_str())
        return
    try:
        s = Session.load(name, llm=ctx.agent.llm, workspace=ctx.session.workspace)
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
    "dump_projections": ("agent", _to_bool),
}


def read_config(agent) -> dict:
    """读取所有可配置项的当前值。fallback_chain 以逗号分隔字符串展示。"""
    cfg = {k: getattr(agent if tgt == "agent" else agent.llm, k)
           for k, (tgt, _) in CONFIGURABLE.items()}
    base_chain = getattr(agent.llm, "_base_fallback_chain", [])
    eff_chain = getattr(agent.llm, "fallback_chain", [])
    cfg["fallback_chain"] = ",".join(base_chain) if base_chain else ""
    cfg["_effective_chain"] = " → ".join(eff_chain) if eff_chain else ""
    try:
        import real_tools
        cfg["tool_timeout"] = real_tools.TOOL_TIMEOUT
    except Exception:
        cfg["tool_timeout"] = 10
    cfg["max_level"] = agent.session.max_level
    cfg["max_effective_context_window"] = agent.llm.max_effective_context_window or 0
    # 步距衰减参数（存 settings.json；detail_base 显示【实际生效值】——显式配置 > 窗口推导 > 1500，
    # 只显示 load_detail_base() 会把推导值误报成 1500）
    try:
        import config as _cfg2
        cfg["detail_base"] = getattr(agent.session, "detail_base", _cfg2.load_detail_base())
        cfg["detail_base_source"] = ("显式" if _cfg2.load_detail_base_opt() else
                                     ("窗口推导" if getattr(agent.session, "max_effective_context_window", None) else "默认"))
        cfg["detail_step"] = _cfg2.load_detail_step()
        cfg["panic_context_window"] = _cfg2.load_panic_window()
        cfg["hook_timeout"] = _cfg2.load_hook_timeout()
        cfg["fold_deep_tools"] = _cfg2.load_fold_deep_tools()
    except Exception:
        cfg["detail_base"] = 1500
        cfg["detail_step"] = 15
        cfg["panic_context_window"] = 0
        cfg["hook_timeout"] = 300
        cfg["fold_deep_tools"] = False
    # 统一辅助模型（存 settings.json；空=跟随主模型）：recap/RAG检索/工作流LLM默认共用
    cfg["utility_model"] = getattr(agent, "utility_model", "") or ""
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
    # fallback_chain 特殊处理（逗号分隔 → 存为 _base_fallback_chain + 重建有效链）
    if "fallback_chain" in values:
        v = values.pop("fallback_chain")
        chain = [m.strip() for m in str(v).split(",") if m.strip()]
        agent.llm._base_fallback_chain = chain
        agent.llm._rebuild_chain()
        try:
            import config
            saved = config.load_runtime_settings(); saved["fallback_chain"] = ",".join(chain); config.save_runtime_settings(saved)
        except Exception:
            pass
        eff = agent.llm.fallback_chain
        results.append(f"✅ fallback_chain = {chain or '(空，无回退)'}"
                       + (f"\n  有效链（{agent.llm._user_model} 在首）：{' → '.join(eff)}" if eff else ""))
        if getattr(agent.llm, "_fallback_owned", False):
            results.append("  ⚠️ 本 agent 的回退链由 .yml 声明锁定（fallback:），此次为运行时临时覆盖，重启后恢复 yml 声明；"
                           "子 Agent 若在 .yml 声明了 fallback 则不受此全局设置影响")
    # max_level：分档最高级别（设 session + 存 settings.json；改了清冻结缓存让其按新上限重算）
    if "max_level" in values:
        v = values.pop("max_level")
        try:
            ml = int(str(v).strip()) if str(v).strip() else 4
            if ml < 1:
                ml = 4
            agent.session.max_level = ml
            agent.session._frozen_renders.clear()
            try:
                import config
                saved = config.load_runtime_settings(); saved["max_level"] = ml; config.save_runtime_settings(saved)
            except Exception as e:
                results.append(f"⚠️ max_level 已设为 {ml}，但持久化失败：{e}")
            results.append(f"✅ max_level = {ml}（分档最高级别；已存 settings.json）")
        except Exception:
            results.append(f"❌ max_level 值非法：{v}")
    # max_effective_context_window：分档投影窗口（当前模型 llm+session + 存 models.json；0/空=关闭分档）
    if "max_effective_context_window" in values:
        v = values.pop("max_effective_context_window")
        try:
            win = int(str(v).strip()) if str(v).strip() else 0
            agent.llm.max_effective_context_window = win or None
            agent.session.max_effective_context_window = win or None
            agent.session._frozen_renders.clear()
            try:
                import config
                name = agent.llm.model_name
                if name in config.MODELS:
                    if win:
                        config.MODELS[name]["max_effective_context_window"] = win
                    else:
                        config.MODELS[name].pop("max_effective_context_window", None)
                    config.save_user_models(config.MODELS, config.DEFAULT_MODEL)
            except Exception as e:
                results.append(f"⚠️ max_effective_context_window 已设为 {win or '关闭'}，但持久化失败：{e}")
            results.append(f"✅ max_effective_context_window = {win or 'None（关闭分档→原窗口+摘要）'}（已存 models.json[{agent.llm.model_name}]）")
        except Exception:
            results.append(f"❌ max_effective_context_window 值非法：{v}")
    # 统一辅助模型（存 settings.json + 即时生效；空=跟随主模型）：
    # recap 总结 / RAG 检索 / reasoning 补全默认 / 工作流 LLM/意图节点默认 全走它
    if "utility_model" in values:
        v = values.pop("utility_model")
        try:
            import config
            um = str(v).strip()
            saved = config.load_runtime_settings()
            saved["utility_model"] = um
            # 旧字段并入后清掉（retrieval_model / recap_model 已废弃）
            saved.pop("retrieval_model", None)
            saved.pop("recap_model", None)
            config.save_runtime_settings(saved)
            agent.utility_model = um
            agent._utility_llm = None   # 清缓存，下次 utility_client 按新值惰性重建
            agent.retrieval_llm = agent.utility_client()
            results.append(f"✅ utility_model = {um or '（跟随主模型）'}"
                           "（recap/RAG检索/工作流LLM默认共用；已存 settings.json + 即时生效）")
        except Exception as e:
            results.append(f"⚠️ utility_model 设置失败：{e}")
    # hook_timeout：同步钩子工作流超时秒数（存 settings.json + 即时生效——_run_hooks 每次实时读）
    if "hook_timeout" in values:
        v = values.pop("hook_timeout")
        try:
            val = max(0, int(str(v).strip() or 300))
            import config
            saved = config.load_runtime_settings(); saved["hook_timeout"] = val
            config.save_runtime_settings(saved)
            results.append(f"✅ hook_timeout = {val or '0（不限时）'}s（已存 settings.json + 即时生效；async 钩子不受限）")
        except Exception as e:
            results.append(f"⚠️ hook_timeout 设置失败：{e}")
    # panic_context_window：轮内保命阀阈值（存 settings.json + 即时生效——_build 每次 build 实时读）
    # 0 = 跟随 max_effective_context_window；设高（如总窗口 80%）则 75%×win~panic 之间纯追加
    if "panic_context_window" in values:
        v = values.pop("panic_context_window")
        try:
            val = max(0, int(str(v).strip() or 0))
            import config
            saved = config.load_runtime_settings(); saved["panic_context_window"] = val
            config.save_runtime_settings(saved)
            results.append(f"✅ panic_context_window = {val or '0（跟随分档窗口）'}"
                           f"（已存 settings.json + 即时生效）")
        except Exception as e:
            results.append(f"⚠️ panic_context_window 设置失败：{e}")
    # fold_deep_tools：超深档工具调用整体折叠（存 settings.json + 即时生效——冻结缓存 key 含 fold 位，
    # 切换时清缓存让全部轮按新形态重渲染）
    if "fold_deep_tools" in values:
        v = values.pop("fold_deep_tools")
        try:
            val = _to_bool(v)
            import config
            saved = config.load_runtime_settings(); saved["fold_deep_tools"] = val
            config.save_runtime_settings(saved)
            agent.session._frozen_renders.clear()
            results.append(f"✅ fold_deep_tools = {val}"
                           + ("（超深档轮的工具调用折叠成一行标注，保留回复+reasoning 原文）" if val
                              else "（超深档轮按 max_level 逐调用残缺摘要）")
                           + "（已存 settings.json + 即时生效）")
        except Exception as e:
            results.append(f"⚠️ fold_deep_tools 设置失败：{e}")
    # detail_base / detail_step：步距衰减参数（存 settings.json + 改 toollog 模块变量即时生效）
    for _dk in ("detail_base", "detail_step"):
        if _dk in values:
            v = values.pop(_dk)
            try:
                val = int(str(v).strip()) if str(v).strip() else (1500 if _dk == "detail_base" else 15)
                import config, toollog
                saved = config.load_runtime_settings(); saved[_dk] = val; config.save_runtime_settings(saved)
                if _dk == "detail_base":
                    toollog.set_detail_params(base=val)
                    # Session 侧统一入口也失效（投影消费点全部走 session.detail_base；
                    # toollog 模块变量保留给 set_detail_params 旧路径，但不再是唯一真相）
                    try:
                        agent.session.invalidate_detail_base()
                    except Exception:
                        pass
                    results.append(f"✅ detail_base={val}（已存 settings.json + 即时生效；"
                                   f"当前投影生效值={getattr(agent.session, 'detail_base', val)}）")
                else:
                    toollog.set_detail_params(step=val)
                results.append(f"✅ {_dk} = {val}（已存 settings.json + 即时生效）")
            except Exception as e:
                results.append(f"⚠️ {_dk} 设置失败：{e}")
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
        print("用法：/config <key> <value> [<key> <value> ...]；可配置：" + " / ".join(CONFIGURABLE)
              + " / max_level / max_effective_context_window")
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


def _cmd_stats(ctx: CommandContext, args):
    """LLM 调用可靠性统计（per-model：调用/成功/空/截断/错误/completer/tokens/均耗时）。
    默认看当前 session；`/stats all` 看本仓库全部 session 聚合。"""
    from llm_call_log import format_stats, load_all_calls
    from session import _repo_sessions_dir
    arg = (args or "").strip().lower()
    if arg == "all":
        records = load_all_calls(_repo_sessions_dir(ctx.agent.session.workspace))
        title = "LLM 调用统计（本仓库全部 session）"
    else:
        records = ctx.agent.session.llm_calls.all_records()
        title = f"LLM 调用统计（当前 session：{ctx.agent.session.name or '(未命名)'}）"
    print(format_stats(records, title))


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
    ctx.agent.switch_model(name, _user_initiated=True)
    m = MODELS[name]
    print(f"✅ 已切换到 {name}: {m['model']} @ {m['base_url']}")
    chain = getattr(ctx.agent.llm, "fallback_chain", [])
    if chain:
        print(f"  有效回退链：{' → '.join(chain)}")


def _cmd_autonomous(ctx: CommandContext, args):
    """纯自主模式控制：/autonomous on <时间> /autonomous off /autonomous status"""
    from datetime import datetime, timedelta

    if not args:
        # 显示状态
        if not ctx.agent.autonomous_mode:
            print("纯自主模式：未开启（输入 /autonomous on <时间> 开启；详细用法见 /help）")
        elif ctx.agent.is_autonomous_active():
            print(f"纯自主模式：已开启，持续到 {ctx.agent.autonomous_end_time.strftime('%Y-%m-%d %H:%M')}")
            print(f"自动提示词：{ctx.agent.autonomous_prompt}")
            print(f"待处理消息队列：{len(ctx.agent.pending_messages)} 条")
        else:
            print("纯自主模式：已超时（自动关闭）")
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


def _coerce(v):
    """把命令行字符串粗略转成配置值：bool / int / 逗号 list / 其余字符串。"""
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if "," in s:
        return [x.strip() for x in s.split(",") if x.strip()]
    if s.lstrip("-").isdigit():
        return int(s)
    return s


# ========== 内嵌 Web 服务（/web） ==========

def _cmd_web(ctx: CommandContext, args):
    """/web [start] [port] | stop | status —— 按需启停内嵌 Web 服务。
    无参或 start：启动（默认 8000）+ 自动开浏览器 + 打印本机/局域网地址。
    /web 9000 或 /web start 9000：指定端口；/web stop：释放端口；/web status：查状态。"""
    from server import start_server, stop_server, server_status, open_browser, lan_urls
    ws = ctx.session.workspace
    sub = args[0].lower() if args else "start"

    if sub == "stop":
        ok, msg = stop_server()
        print(("🛑 " if ok else "⚠️ ") + msg)
        return
    if sub == "status":
        st = server_status()
        if not st["running"]:
            print("服务未运行")
        else:
            print(f"服务运行中 @ 0.0.0.0:{st['port']}")
            print(f"  本机:   {st['local_url']}")
            print(f"  局域网: {', '.join(st['lan_urls'])}")
            if st.get("error"):
                print(f"  错误: {st['error']}")
        return

    if ctx.work_q is None:
        print("⚠️ 此命令需在 CLI（chat 主循环）启动；Web 端无需再启动服务。")
        return
    # 启动（默认端口）
    port = 8000
    if sub.isdigit():
        port = int(sub)
    if len(args) >= 2 and args[1].isdigit():
        port = int(args[1])
    ok, msg = start_server(agent=ctx.agent, work_q=ctx.work_q,
                           mcp_mgr=getattr(ctx.agent, "mcp_mgr", None),
                           workspace=ws, port=port, state=getattr(ctx, "state", None))
    if not ok:
        print(f"❌ {msg}")
        return
    print(f"✅ {msg}")
    print(f"  本机:   http://127.0.0.1:{port}/")
    print(f"  局域网: {', '.join(lan_urls(port))}")
    print("  （局域网内任何设备可连并驱动 Agent，仅在可信网络使用；/web stop 释放端口）")
    open_browser(port)


# ========== 快照回溯（/snapshot） ==========

def _cmd_snapshot(ctx: CommandContext, args):
    """/snapshot list | restore <序号|sha> —— 工作区文件快照回溯（检查点）。
    每轮对话开始前自动打一个快照；restore 回到该快照之前（截断对话 + 还原文件树）。"""
    from chat import get_snapshot_list, restore_snapshot
    positional = _parse_args(args)[0]
    sub = positional[0].lower() if positional else "list"
    if ctx.agent.snapshot_manager is None:
        print("（快照未启用）")
        return
    items = get_snapshot_list(ctx.session)   # 按时间正序（旧→新）

    if sub in ("list", "ls"):
        if not items:
            print("（暂无快照点；每轮对话开始时会自动打一个快照）")
            return
        print(f"📸 快照点（{len(items)} 个，序号大的=最近）：")
        for n, it in enumerate(items, 1):
            msg = (it["user_message"] or "").replace("\n", " ")[:40]
            print(f"  #{n}  {it['sha'][:10]}  「{msg}」")
        print("用法：/snapshot restore <序号|sha>")
        return

    if sub == "restore":
        if len(positional) < 2:
            print("用法：/snapshot restore <序号|sha>")
            return
        key = positional[1]
        sha = None
        if key.isdigit():
            n = int(key)
            if 1 <= n <= len(items):
                sha = items[n - 1]["sha"]
        if sha is None:
            matches = [it["sha"] for it in items if it["sha"].startswith(key)]
            if len(matches) == 1:
                sha = matches[0]
            elif not matches:
                print(f"❌ 找不到快照 {key}")
                return
            else:
                print(f"❌ 前缀 {key} 匹配多个快照，请用更长的前缀或序号")
                return
        try:
            target = restore_snapshot(ctx.agent, sha)
        except Exception as e:
            print(f"❌ 回溯失败：{type(e).__name__}: {e}")
            return
        if target is None:
            print("❌ 回溯未生效（对话中找不到该快照点）")
        else:
            print(f"✅ 已回溯到该快照之前（截掉的轮：「{target[:60]}」）")
        return

    print(f"❌ 未知子命令 {sub}；可用：list / restore")


def _cmd_rewind(ctx: CommandContext, args):
    """/rewind [count] —— 回溯到 count 个 turn 之前：撤销最近 count 轮（对话 + 文件改动），count 默认 1。
    依赖每轮自动打的工作区快照；回到指定轮【发送前】的状态。"""
    from chat import restore_snapshot
    if ctx.agent.snapshot_manager is None:
        print("（快照未启用，无法回溯）")
        return
    turns = ctx.session.turns
    if not turns:
        print("（暂无对话轮，无可回溯）")
        return
    count = 1
    if args and args[0].isdigit():
        count = max(1, int(args[0]))
    n = len(turns)
    if count > n:
        print(f"⚠️ 共 {n} 轮，回溯全部（回到最初）")
        count = n
    target = turns[n - count]               # 倒数第 count 轮 = 撤销起点
    sha = getattr(target, "snapshot_sha", "") or ""
    if not sha:
        print(f"❌ 倒数第 {count} 轮没有快照点，无法回溯")
        return
    try:
        restore_snapshot(ctx.agent, sha)
    except Exception as e:
        print(f"❌ 回溯失败：{type(e).__name__}: {e}")
        return
    remain = len(ctx.session.turns)
    print(f"✅ 已回溯（撤销最近 {count} 轮的对话 + 文件改动），剩余 {remain} 轮")


# ========== RAG 文档库（/rag） ==========

def _cmd_rag(ctx: CommandContext, args):
    """/rag build | config [key val...] | stats | query <词> —— RAG 文档库管理。"""
    from rag import get_rag
    from config import load_rag_config, save_rag_config
    import chat as chatmod
    ws = ctx.session.workspace
    positional, _flags = _parse_args(args)
    sub = positional[0].lower() if positional else "stats"

    if sub == "config":
        rest = positional[1:]
        if not rest:
            cfg = load_rag_config(ws)
            for k, v in cfg.items():
                print(f"  {k} = {v}")
            print("用法：/rag config <key> <value> [<key> <value> ...]")
            return
        if len(rest) % 2 != 0:
            print("❌ 参数须成对 key value")
            return
        cfg = load_rag_config(ws)
        for i in range(0, len(rest), 2):
            cfg[rest[i]] = _coerce(rest[i + 1])
        save_rag_config(ws, cfg)
        chatmod.init_rag(ws)
        print(f"✅ 已保存 {len(rest) // 2} 项并重建 RAG 实例")
        return

    if sub == "build":
        inst = get_rag()
        cfg = load_rag_config(ws)
        if inst is None:
            print("❌ RAG 未启用或 embed_model_path 无效，先 /rag config 配置")
            return
        docs_dir = cfg.get("docs_dir", "")
        if not docs_dir or not Path(docs_dir).exists():
            print(f"❌ docs_dir 不存在：{docs_dir}（/rag config docs_dir <路径>）")
            return
        print(f"📦 建库中：{docs_dir}（同步执行，按文件打印进度）…")

        def on_progress(done, total, f):
            print(f"  [{done}/{total}] {f}")
        res = inst.index_dir(
            docs_dir,
            exts=tuple(cfg.get("exts") or [".md", ".txt", ".json"]),
            exclude_globs=cfg.get("exclude_globs") or [],
            lines_per=cfg.get("lines_per", 60),
            overlap=cfg.get("overlap", 15),
            batch=cfg.get("batch", 32),
            on_progress=on_progress,
        )
        print(f"✅ 完成：{res.get('files')} 文件 / {res.get('chunks')} 片段 / {res.get('elapsed', 0):.1f}s")
        return

    if sub == "stats":
        inst = get_rag()
        if inst is None:
            print("（RAG 未启用，/rag config 配置后 /rag build 建库）")
            return
        st = inst.stats()
        print(f"ready={st['ready']}  docs={st['total_docs']}  dim={st['dim']}")
        return

    if sub == "query":
        rest = positional[1:]
        if not rest:
            print("用法：/rag query <关键词>")
            return
        inst = get_rag()
        if inst is None or inst.index.ntotal == 0:
            print("（索引未建立，先 /rag build）")
            return
        hits = inst.query(" ".join(rest))
        if not hits:
            print("(无匹配)")
            return
        print(f"找到 {len(hits)} 条：")
        for h in hits:
            fp = Path(h["file_path"]).name
            text = h["text"][:80].replace("\n", " ")
            print(f"  {fp}:{h['start_line']}-{h['end_line']}  {text}")
        return

    print(f"❌ 未知子命令 {sub}；可用：build / config / stats / query")


def _cmd_tools(ctx: CommandContext, args):
    """/tools [关键词] —— 列出当前注册的全部工具（按来源前缀分组）。
    无关键词（或 /tools list）列全部；给关键词则按名字/描述过滤。
    Toolbox 按名字去重，总数即唯一工具数（无重复）。"""
    args_clean = [a for a in args if a.lower() != "list"]
    kw = " ".join(args_clean).strip().lower()
    tools = sorted(ctx.agent.tools, key=lambda t: t.name)
    if kw:
        tools = [t for t in tools if kw in t.name.lower() or kw in (t.description or "").lower()]
    if not tools:
        print(f"(无匹配 '{kw}' 的工具)")
        return
    groups = {"MCP": [], "工作流(wf_)": [], "LSP(cs_/py_)": [], "内置/其它": []}
    for t in tools:
        n = t.name
        if n.startswith("__mcp__"):
            groups["MCP"].append(n)
        elif n.startswith("wf_"):
            groups["工作流(wf_)"].append(n)
        elif n.startswith(("cs_", "py_")):
            groups["LSP(cs_/py_)"].append(n)
        else:
            groups["内置/其它"].append(n)
    print(f"🔧 工具 {len(tools)} 个（按名字去重，无重复）：")
    for g, names in groups.items():
        if names:
            print(f"  [{g}] {len(names)} 个：{', '.join(names)}")


def _cmd_call(ctx: CommandContext, args):
    """/call [yes] tool_name({"arg":"val"})  手动调用工具。
    yes = 结果以 user 消息注入 Agent 上下文（进投影，模型下一步能看到）；
    不加 yes = 纯调用，结果仅显示，不进投影。"""
    import re as _re
    import json as _json
    raw = (args or "").strip()
    if not raw:
        print('用法：/call [yes] tool_name({"arg":"val"})')
        print('  yes = 工具调用结果注入 Agent 上下文（模型能看到）')
        print('  不加 yes = 纯调用，结果仅显示，不影响 Agent')
        print('示例：/call read_file({"path":"package.json"})')
        print('      /call yes grep({"pattern":"TODO","path":"."})')
        return
    # 解析 yes 标志
    inject = False
    if raw.startswith("yes ") or raw == "yes":
        inject = True
        raw = raw[3:].strip()
    if not raw:
        print("❌ /call yes 后面要跟工具调用")
        return
    # 解析 tool_name(args_json)
    m = _re.match(r'([\w_]+)\s*\((.*)\)\s*$', raw, _re.DOTALL)
    if not m:
        print(f'❌ 格式错误：应为 tool_name({{"arg":"val"}})，收到：{raw[:80]}')
        return
    tool_name, args_str = m.group(1), m.group(2).strip()
    try:
        tool_args = _json.loads(args_str) if args_str else {}
    except _json.JSONDecodeError as e:
        print(f'❌ 参数 JSON 解析失败: {e}')
        return
    # 检查工具存在
    if tool_name not in ctx.agent.tools:
        print(f'❌ 工具 {tool_name!r} 不存在，/tools 查看可用工具')
        return
    # 调用
    result = ctx.agent.tools.call(tool_name, tool_args)
    record = f"用户手动调用了工具：\n{tool_name}({args_str})\nresult:\n{result}"
    if inject:
        # 注入 Agent 上下文：作为 user 消息进 work_q（模型下一步能看到）
        if ctx.work_q:
            ctx.work_q.put(("user", record))
            print(f"✅ {tool_name} 已调用，结果已注入 Agent 上下文")
        else:
            print(record)
    else:
        # 纯调用：不进投影（CLI 直接打印；WS 端 redirect_stdout 自动捕获成 system 事件）
        print(record)


def _cmd_update(ctx: CommandContext, args):
    """/update —— 检查 PyPI 是否有新版 agt-agent，有则升级（绕过 24h 节流）。
    editable / 本地 / 源码安装会跳过；auto_update 关时只提示不升级。"""
    from updater import check_and_update
    check_and_update(force=True, announce=print)


def _cmd_debug(ctx: CommandContext, args):
    """/debug prompt <提示词> —— 提示词调试：按【当前上下文投影 + 提示词】直接调 LLM，
    不落盘（不 start_turn，session/events 零写入）、不执行（回包的 tool_calls 只展示不跑），
    打印完整回包（耗时/finish_reason/usage含缓存命中/content/reasoning/tool_calls）。
    用于调试 system/上下文工程/工具 schema 对模型行为的影响，零副作用。"""
    import json as _json
    import time as _time
    if not args or args[0] != "prompt" or len(args) < 2:
        print("用法：/debug prompt <提示词>   —— 按当前上下文投影直接调 LLM；不落盘、不执行工具，打印完整回包")
        return
    if ctx.state and ctx.state.get("busy"):
        print("⏳ Agent 正在处理任务（busy），/debug 需在空闲时使用——避免与主循环并发调 LLM")
        return
    text = " ".join(args[1:]).strip()
    agent = ctx.agent
    # 投影快照 + 提示词（不 start_turn → 无 turn_start 事件、无落盘；llm_calls.jsonl 仍会记录本次调用，供 /stats 观测）
    msgs = list(agent.session.messages_for_llm()) + [{"role": "user", "content": text}]
    proj_chars = sum(len(str(m.get("content") or "")) for m in msgs)
    n_tools = len(agent.tools.schemas())
    print(f"🧪 [debug prompt] 不落盘 · 不执行")
    print(f"   投影：{len(msgs)} 条消息 / {proj_chars} 字符（含本次提示词 {len(text)} 字）")
    print(f"   模型：{agent.llm.model_name} ({agent.llm.model}) · 工具 schema {n_tools} 个")
    print("─" * 56)
    t0 = _time.time()
    try:
        resp = agent.utility_client().chat(msgs, tools=agent.tools.schemas(), scene="debug")
    except Exception as e:
        print(f"❌ 调用失败: {type(e).__name__}: {e}")
        return
    dt = _time.time() - t0
    u = resp.usage or {}
    ptd = (u.get("prompt_tokens_details") or {}) if isinstance(u, dict) else {}
    cached = ptd.get("cached_tokens") or 0
    prompt_t = u.get("prompt_tokens") or 0
    cache_pct = f"（缓存命中 {cached}/{prompt_t} = {cached * 100 // prompt_t}%）" if prompt_t else ""
    print(f"⏱  耗时 {dt:.1f}s · finish_reason = {resp.finish_reason}")
    print(f"📊 tokens: prompt={prompt_t} completion={u.get('completion_tokens')} total={u.get('total_tokens')} {cache_pct}")
    if resp.reasoning:
        print("─" * 56)
        print(f"💭 reasoning（{len(resp.reasoning)} 字）:")
        print(resp.reasoning)
    if resp.tool_calls:
        print("─" * 56)
        print(f"🔧 tool_calls（{len(resp.tool_calls)} 个，仅展示不执行）:")
        for i, tc in enumerate(resp.tool_calls):
            print(f"  [{i}] {tc['name']}({tc.get('id')})")
            print("      " + _json.dumps(tc.get("arguments"), ensure_ascii=False, indent=2).replace("\n", "\n      "))
    print("─" * 56)
    print(f"💬 content（{len(resp.content or '')} 字）:")
    print(resp.content or "(空)")
    print("─" * 56)
    print("（本次调用已记入 llm_calls.jsonl，/stats 可查；session 未受影响）")


def _cmd_reload(ctx: CommandContext, args):
    """/reload models|tools —— 热重载。
    models：重读 ~/.agt/models.json 并热应用（主 llm 同名 profile / utility 通道重建）；
    tools：重扫 tools/ 与 .agent/tools/ 的脚本工具（改 agt_register 脚本后秒级生效，免重启）。"""
    sub = args[0].lower() if args else ""
    if sub == "tools":
        from script_tools import reload_script_tools, _LAST
        print(reload_script_tools(ctx.agent))
        for nm in _LAST["names"]:
            ctx.agent.tool_groups[nm] = "脚本"
        return
    if sub != "models":
        print("用法：/reload models   —— 重读模型配置并热应用（改 models.json 后免重启生效）")
        print("      /reload tools    —— 重扫脚本工具目录并热应用（改 tools/*.py 后免重启生效）")
        return
    import config
    config.reload_models()
    print(f"✅ 模型配置已重载：{len(config.MODELS)} 个条目（默认 {config.DEFAULT_MODEL or '无'}）")
    agent = ctx.agent
    cur = agent.llm.model_name
    try:
        if cur in config.MODELS:
            agent.llm._apply_profile(config.get_profile(cur))
            print(f"  · 主模型 '{cur}' profile 已刷新（model={agent.llm.model}，tokens={len(agent.llm.api_tokens)} 个）")
        else:
            print(f"  ⚠️ 当前模型 '{cur}' 不在新配置中——保持旧 profile，可用 /model 切换")
    except Exception as e:
        print(f"  ⚠️ 主模型 profile 刷新失败：{e}")
    # utility 通道：清缓存按新 MODELS 重建（utility_model 设置值不变）
    agent._utility_llm = None
    try:
        agent.retrieval_llm = agent.utility_client()
        um = getattr(agent, "utility_model", "")
        print(f"  · utility 通道已重建（utility_model={um or '跟随主模型'}）")
    except Exception as e:
        print(f"  ⚠️ utility 通道重建失败：{e}")


def _cmd_exit(ctx: CommandContext, args):
    """/exit（= /quit）—— 优雅退出：等当前任务完成后退出程序。
    与 CLI 裸词 quit/exit/q/退出 等价；斜杠形态在 WebUI/stdin 驱动等所有入口都可用。
    ⚠️ agt-web 模式下会关掉整个服务（所有客户端断连）——远程关服入口。"""
    print("👋 再见！（/exit：等当前任务完成后退出）")
    q = getattr(ctx, "work_q", None)
    if q is None:
        q = getattr(ctx.agent, "_work_q", None)   # 兜底：WebUI dispatch 漏传 work_q 时
    if q is not None:
        q.put(None)   # 哨兵：worker 完成当前项后退出 → 主循环 finally 清理（停服务/防孤儿进程）
    else:
        print("⚠️ 无 work_q 通道，请在终端 Ctrl+C 退出")


def _cmd_restart(ctx: CommandContext, args):
    """/restart [消息] —— 看门狗式重启：本进程优雅退出 → 看门狗拉起新进程（新代码生效）
    → 自动恢复当前 session 与 Web 服务端口 → （可选）把剩余参数作为重启后第一条消息发送。
    适用：改了 agt 源码（src/）或依赖后让修改生效，无需手动 quit/agt/再 /resume。"""
    import os
    from restart_watchdog import spawn_watchdog
    from real_tools import WORKSPACE
    message = " ".join(args).strip() if isinstance(args, list) else str(args or "").strip()
    port = 0
    try:
        import server as _srv
        if getattr(_srv, "_server", None) is not None:
            port = _srv._port or 0
    except Exception:
        pass
    mode = "web" if port else "cli"
    ok, info = spawn_watchdog(parent_pid=os.getpid(), mode=mode,
                              session=ctx.session.name or "", port=port,
                              message=message, cwd=str(WORKSPACE))
    print(info)
    if not ok:
        return
    print("（日志：~/.agt/restart.log）")
    # 优雅退出哨兵：worker/渲染循环按正常 quit 路径清理（停服务/杀后台进程）。
    # WebUI 路径的 ctx 可能没带 work_q（构造点历史不一致）→ 兜底 agent._work_q（main/web_main 均挂载）
    wq = ctx.work_q or getattr(ctx.agent, "_work_q", None)
    if wq is not None:
        # 丢弃排队未处理的消息：哨兵排队尾等排空的话，一条后台通知+轮末自动工作流
        # 就能把退出拖过看门狗等父进程的窗口（重启失败服务下线）。正在跑的当前轮
        # 仍会完成（回答用户能看到）。drain 后到 put 之间新入队的项会在哨兵前被处理，
        # 毫秒级竞态窗口，/restart 场景可忽略。
        import queue as _q
        dropped = 0
        while True:
            try:
                _item = wq.get_nowait()
            except _q.Empty:
                break
            if _item is not None:
                dropped += 1
        if dropped:
            print(f"🗑️ 已丢弃 {dropped} 条排队消息（重启不等排空，只等当前轮完成）")
        wq.put(None)
    else:
        print("⚠️ 找不到 work_q，无法自动退出——请手动关闭本进程（看门狗已在等）")


# ========== 实例状态（/status） ==========

def _child_procs(pid: int) -> list:
    """枚举 pid 的直接子进程 [(pid, name, cmdline)]。Windows wmic / POSIX ps，失败返回空。"""
    import os as _os
    import subprocess as _sp
    try:
        if _os.name == "nt":
            r = _sp.run(
                ["wmic", "process", "where", f"ParentProcessId={pid}",
                 "get", "ProcessId,Name,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            out = []
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or "," not in line:
                    continue
                parts = line.split(",")
                # CSV: Node,CommandLine,Name,ProcessId —— CommandLine 可含逗号，从右解析
                try:
                    cpid = int(parts[-1])
                    name = parts[-2]
                    cmd = ",".join(parts[1:-2])
                    out.append((cpid, name, cmd))
                except (ValueError, IndexError):
                    continue
            return out
        r = _sp.run(["ps", "-o", "pid=,comm=,args=", "--ppid", str(pid)],
                    capture_output=True, text=True, timeout=15)
        return [(int(p[0]), p[1], p[2]) for p in
                (l.strip().split(None, 2) for l in r.stdout.splitlines()) if len(p) == 3]
    except Exception:
        return []


def _proc_tree(max_depth: int = 3) -> list:
    """本进程的子进程树（递归 max_depth 层）。返回 [(depth, pid, name, cmdline)]。
    诊断 run_shell 卡死用：cmd→powershell→GUI 安装器这类孙进程链直接可见。"""
    import os as _os
    rows = []

    def walk(pid, depth):
        if depth > max_depth:
            return
        for cpid, name, cmd in _child_procs(pid):
            rows.append((depth, cpid, name, cmd))
            walk(cpid, depth + 1)

    walk(_os.getpid(), 1)
    return rows


def _cmd_status(ctx: CommandContext, args):
    """/status —— 实例状态总览：模型/队列/会话 + 子进程树(PID+命令行) + 后台服务/任务 + 团队 + 钩子。
    诊断卡死/挂起首选：busy 但长时间无进展时看进程树——run_shell 启动的卡住进程一目了然
    （上次 8000 实例卡 88 分钟就是 GUI 安装器挂在孙进程上，taskkill <pid> 即可解围）。"""
    import os as _os
    a = ctx.agent
    st = ctx.state or {}
    print("=" * 64)
    print(f"🩺 实例状态（PID {_os.getpid()}）")
    print("=" * 64)
    # —— 基本 ——
    try:
        wq = ctx.work_q.qsize() if ctx.work_q is not None else "?"
    except Exception:
        wq = "?"
    try:
        import real_tools as _rt
        timeout = _rt.TOOL_TIMEOUT
    except Exception:
        timeout = "?"
    print(f"会话: {ctx.session.name or '(未命名)'}（{len(ctx.session.turns)} 轮）")
    print(f"模型: {a.model_name}  utility={getattr(a, 'utility_model', '') or '(跟随主)'}")
    print(f"运行: busy={st.get('busy')}  交互目标={getattr(a, '_active_target', '_main_')}")
    print(f"队列: work_q={wq}  inbox={len(getattr(a, 'inbox', []) or [])}"
          f"  pending={len(getattr(a, 'pending_messages', []) or [])}  tool_timeout={timeout}s")
    # —— 子进程树（卡死诊断核心）——
    print(f"\n🌳 子进程树（本实例之下，≤3 层；卡住的可 taskkill <pid> 解围）：")
    tree = _proc_tree()
    if not tree:
        print("  (无子进程)")
    for depth, pid, name, cmd in tree:
        print(f"  {'  ' * depth}[{pid}] {(cmd or name).replace(chr(10), ' ')[:110]}")
    # —— 后台服务 ——
    svcs = getattr(a, "services", None)
    if svcs is not None:
        print("\n⚙️ 后台服务：")
        try:
            print(svcs.list())
        except Exception as e:
            print(f"  (读取失败: {e})")
    # —— 定时任务 ——
    sched = getattr(a, "scheduler", None)
    if sched is not None:
        print("\n⏰ 定时任务：")
        try:
            print(sched.list())
        except Exception:
            print("  (读取失败)")
    # —— 后台子 Agent 任务 ——
    bts = getattr(a, "background_tasks", None) or {}
    if bts:
        print(f"\n📋 后台任务（{len(bts)}）：")
        for aid, t in list(bts.items())[:8]:
            print(f"  {aid:<14} {t.get('status', '?')}")
        if len(bts) > 8:
            print(f"  ...（其余 {len(bts) - 8} 个略）")
    # —— 团队（紧凑）——
    reg = getattr(a, "registry", None)
    if reg is not None:
        with reg._lock:
            entries = list(reg._agents.values())
        subs = [e for e in entries if e.role == "subagent"]
        running = sum(1 for e in subs if e.status == "running")
        print(f"\n👥 团队：{len(subs)} 个子 Agent（运行中 {running}）")
        for e in subs[:8]:
            rc = (e.recap or e.task or "")[:36]
            print(f"  {e.agent_id:<12} {e.status:<8} {rc}")
        if len(subs) > 8:
            print(f"  ...（其余 {len(subs) - 8} 个略，/agent 看全部）")
    # —— 钩子工作流 ——
    try:
        from real_tools import WORKSPACE as _ws
        from workflow import get_hook_workflows
        hws = get_hook_workflows(_ws)
        if hws:
            print(f"\n🪝 钩子工作流（{len(hws)}）：")
            for hw in hws:
                m = hw.get("meta") or {}
                flags = "/".join(f for f, k in (("async", "async"), ("recap", "recap")) if m.get(k))
                fl = f"（{flags}）" if flags else ""
                print(f"  {m.get('hook', '?'):<14} {hw['name']}{fl}")
    except Exception:
        pass
    print("=" * 64)


def _cmd_context(ctx: CommandContext, args):
    """/context —— 上下文堆积情况：最近 react 的实际 prompt tokens + 投影各段估算占比。
    段落：system / rules / 长期记忆·静态 / 折叠摘要 / 各档历史 / global_summary / 当前轮 / tail。"""
    import time as _time
    s = ctx.agent.session
    # 失效 base 缓存重读（直改 settings.json 后 /context 立即反映新配置——显式值变化场景）
    try:
        s.invalidate_detail_base()
    except Exception:
        pass

    # ① 最近一次 react 调用（实际 token，来自 llm_calls）
    last_react = None
    try:
        for r in reversed(s.llm_calls.all_records()):
            if r.get("scene") == "react" and r.get("outcome") == "success":
                last_react = r
                break
    except Exception:
        pass

    print(f"📊 上下文堆积「{s.name or '(未命名)'}」（{len(s.turns)} 轮完成"
          + (f" + 进行中第{len(s.turns)+1}轮" if s._current is not None else "") + "）")
    if last_react:
        u = last_react.get("usage") or {}
        pt, ct = u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        age_min = (_time.time() - (last_react.get("ts") or 0)) / 60
        age_s = f"{age_min:.0f}分钟前" if age_min < 90 else f"{age_min/60:.1f}小时前"
        endpoint = last_react.get("resp_model") or last_react.get("model") or "?"
        hit_pct = cached * 100 // max(pt, 1)
        print(f"最近 react 调用（{age_s}，{endpoint}）: "
              f"prompt {pt:,} tok | cached {cached:,} ({hit_pct}%) | completion {ct:,}")
    else:
        print("（本 session 尚无成功的 react 调用记录）")

    # ② 分段估算（优先 live：真实装配时顺手记录的 _proj_stats；无则现算兜底）
    bd = s.projection_breakdown()
    if bd.get("source") == "live":
        age2 = _time.time() - bd.get("ts", 0)
        age2s = f"{age2:.0f}秒前" if age2 < 90 else (f"{age2/60:.0f}分钟前" if age2 < 5400 else f"{age2/3600:.1f}小时前")
        print(f"段落统计（采自上次真实投影 t{bd.get('turn')}·s{bd.get('step')}，{age2s}）")
    else:
        print("段落统计（现算估算——本进程尚未跑过投影）")
    total = max(bd["total_tokens"], 1)
    win = s.max_effective_context_window
    panic = 0
    fold_on = False
    try:
        import config as _cfg
        panic = _cfg.load_panic_window()
        fold_on = _cfg.load_fold_deep_tools()
    except Exception:
        pass
    if win:
        pct = bd["total_tokens"] * 100 / win
        bar = "▓" * min(int(pct // 5), 30)
        print(f"\n投影估算: ~{bd['total_tokens']:,} tok / 分档窗口 {win:,} ({pct:.1f}%) {bar}")
        if panic:
            print(f"保命线: {panic:,}（轮内超此线才应急折叠；当前余量 {max(panic - bd['total_tokens'], 0):,}）")
        print(f"折叠计划: _planned_fold={s._planned_fold} 轮 | 已毕业边界 {len(s._tier_boundaries)} 个"
              f" | max_level={s.max_level} | fold_deep_tools={'开' if fold_on else '关'}")
    else:
        print(f"\n投影估算: ~{bd['total_tokens']:,} tok（未配 max_effective_context_window，走 recent_window 路径）")
        if s.global_summary:
            print(f"global_summary: {len(s.global_summary)} 字（窗口外轮次摘要）")

    # 段落表
    print("\n段落构成（估算 tok / 占比）：")
    for sec in bd["sections"]:
        p = sec["tokens"] * 100 / total
        bar = "█" * min(int(p * 2), 40)
        meta = f"   ← {sec['meta']}" if sec["meta"] else ""
        print(f"  {sec['name']:<28} {sec['tokens']:>9,}  {p:5.1f}% {bar}{meta}")
    print(f"  {'合计':<28} {bd['total_tokens']:>9,}  100.0%  ({bd['total_chars']:,} 字符 / "
          f"{sum(x['msgs'] for x in bd['sections']):,} 条消息)")
    # 标定系数：估算(chars/4)对中文偏低；有实际 react 值时给出换算比，各段占比不受影响
    if last_react:
        pt = (last_react.get("usage") or {}).get("prompt_tokens") or 0
        if pt and bd["total_tokens"]:
            k = pt / bd["total_tokens"]
            print(f"  （标定：chars/4 低估中文 token，实际/估算 ≈ {k:.2f}×；"
                  f"各段按 {k:.1f}× 折算即实际量级，占比不变）")


def _cmd_update_assets(ctx: CommandContext, args):
    """/update-assets [apply] [--force] —— 随包播种资产对比/热更新（pip 升级后用）。
    无参=预览差异表；apply=执行安全更新（装缺+更新未改项）；--force 连本地已改/未知项也覆盖。
    工具脚本/节点插件直接从随包 assets 目录扫描（升级即新版），不在本命令管辖内。"""
    from asset_sync import update_seed_assets
    sub = [a.lower() for a in (args or [])]
    apply = "apply" in sub
    force = "--force" in sub or "force" in sub
    print(update_seed_assets(apply=apply, force=force, workspace=ctx.session.workspace))


def _cmd_agent(ctx: CommandContext, args):
    """/agent [agent_id] —— 切换直接交互的 Agent 目标。
    无参数：列出所有团队成员（含 agent_id、名称、状态）。
    /agent <id>：切换到与该 Agent 直接交互（仅允许 done/idle 状态的 Agent）。
    /agent _main_：切回主 Agent。"""
    reg = getattr(ctx.agent, "registry", None)
    if not reg:
        print("(多 Agent 通信未启用：无 registry)")
        return
    positional = _parse_args(args)[0]
    if not positional:
        # 列出所有团队成员
        team = reg.format_team(exclude_id="")
        if not team:
            print("(暂无其他活跃 Agent)")
        else:
            print(team)
        cur = getattr(ctx.agent, "_active_target", "_main_")
        print(f"\n当前交互目标：{cur}")
        print("用法：/agent <agent_id>  切换到与该 Agent 直接交互")
        print("      /agent _main_      切回主 Agent")
        return
    target_id = positional[0]
    # 切回主 Agent
    if target_id in ("_main_", "main", "back"):
        ctx.agent._active_target = "_main_"
        print("✅ 已切回主 Agent")
        # 通知 WebUI 刷新
        if ctx.agent.on_event:
            try:
                ctx.agent.on_event({"type": "session_history",
                                    "name": ctx.agent.session.name or "(当前会话)",
                                    "turns": ctx.agent.session.to_history()})
            except Exception:
                pass
        return
    # 切换到子 Agent
    entry = reg.lookup(target_id)
    if entry is None:
        print(f"❌ agent_id='{target_id}' 不在注册表中。用 /agent 查看可用成员。")
        return
    if entry.status == "running":
        print(f"⏳ '{target_id}' 正在执行任务，完成后才能切换直接交互。")
        print("  （可用 wait_subagents 等它完成，或用 agent_ask / agent_notify 与它通信）")
        return
    ctx.agent._active_target = target_id
    print(f"✅ 已切换到与 '{entry.name}' [{target_id}] 直接交互")
    print(f"  模型：{entry.model}，任务：{(entry.task or '(无)')[:60]}")
    # 通知 WebUI 刷新到该 Agent 的 session 历史
    if ctx.agent.on_event and entry.agent:
        try:
            ctx.agent.on_event({"type": "session_history",
                                "name": f"{entry.name} [{target_id}]",
                                "turns": entry.agent.session.to_history()})
        except Exception:
            pass


def build_default_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register("save", _cmd_save,
        "[name]  保存当前会话（日常已自动落盘，此命令用于改名另存或强制再存一次）",
        "/save              强制再存一次（不改名）\n"
        "/save 我的项目     改名另存为新会话")
    reg.register("rename", _cmd_rename,
        "<新名>  重命名当前会话（改名+改存档文件）",
        "/rename 调试工作流\n"
        "/rename 我的 项目 笔记     （可含空格）")
    reg.register("resume", _cmd_resume,
        "<name>  恢复指定会话到内存（历史/计划/自主模式/子Agent 全部恢复）",
        "/resume 我的项目\n"
        "/resume 20260811_013200   （用 /list 查看可用会话 ID）")
    reg.register("list", _cmd_list,
        "列出所有已保存会话（按创建时间倒序）")
    reg.register("show", _cmd_show,
        "[name]  查看会话详情摘要（不传=当前会话）",
        "/show               查看当前会话摘要\n"
        "/show 我的项目      查看指定会话摘要")
    reg.register("recall", _cmd_recall,
        "<关键词>  在全部历史轮次里搜索，召回匹配轮的完整内容（不含思考过程）",
        "/recall server.py\n"
        "/recall 回退链")
    reg.register("reset", _cmd_reset,
        "重置会话（清空历史、计划、自主模式，system 保留）",
        "⚠️ 不可逆！当前对话历史和计划会被全部清除。")
    reg.register("config", _cmd_config,
        "<key> <value> [key value...]  改运行时配置（可同时改多个）",
        "/config                        查看当前所有配置\n"
        "/config max_steps 100          最大步数\n"
        "/config token_budget 100000    token 预算\n"
        "/config temperature 0.5        温度\n"
        "/config enable_thinking true   思考模式开关\n"
        "/config max_level 4            分档最高级别\n"
        "/config detail_base 1500       步距衰减初始字数\n"
        "/config panic_context_window 160000  保命阀阈值（0=跟随分档窗口；轮内超此线才应急折叠，一次压回75%计划水位）\n"
        "/config hook_timeout 300    同步钩子工作流超时秒数（超时结果丢弃不卡主循环；0=不限；async钩子不受限）\n"
        "/config fold_deep_tools true  超深档(超过max_level)轮的工具调用整体折叠成一行标注，保留回复+reasoning原文\n"
        "/config fallback_chain glm,deepseek,qwen   回退链\n"
        "/config dump_projections true  投影转储（调试用）")
    reg.register("budget", _cmd_budget,
        "查看本次运行 token 消耗（已用 / 预算）")
    reg.register("stats", _cmd_stats,
        "[all]  LLM 调用可靠性统计（per-model 成功/空/截断/错误/completer/tokens/均耗时）",
        "/stats           当前 session 统计\n"
        "/stats all       本仓库全部 session 聚合统计")
    reg.register("model", _cmd_model,
        "[name]  列出/切换 LLM 模型",
        "/model           列出所有可用模型（← 标记当前）\n"
        "/model glm       切换到 glm\n"
        "/model deepseek  切换到 deepseek")
    reg.register("reload_mcp", _cmd_reload_mcp,
        "<name>  重连指定 MCP server（代码修改后生效）",
        "/reload_mcp python-lsp     （.mcp.json 中 mcpServers 的键名）")
    reg.register("autonomous", _cmd_autonomous,
        "纯自主模式：任务完成后自动继续工作，直到时间到或目标达成",
        "/autonomous                    查看状态\n"
        "/autonomous on 17:30           到今天 17:30 自动停\n"
        "/autonomous on 2026-08-14 10:00  到指定日期时间停\n"
        "/autonomous duration 30        持续 30 分钟\n"
        "/autonomous off                手动关闭\n"
        "/autonomous status             查看状态\n"
        "/autonomous prompt <文字>       修改自动继续时的提示词\n"
        "/autonomous goal <Python脚本>    设目标验证脚本（print('PASS')=达成→自动停）\n"
        "/autonomous check              手动运行一次目标验证脚本")
    reg.register("workflows", _cmd_workflows,
        "[reload]  列出/重载 .agent/workflows/ 工作流",
        "/workflows          列出所有工作流（含状态/描述/Coze链接）\n"
        "/workflows reload   重新扫描注册（新增/修改的工作流即时生效）")
    reg.register("memory", _cmd_memory,
        "长期记忆管理（跨 session，~/.agt/repos/<hash>/memories/）",
        "/memory                                    概览（三类记忆统计）\n"
        "/memory list [--type T] [--query Q]        列出记忆（可按类型/关键词过滤）\n"
        "/memory show <id>                          查看单条记忆详情\n"
        "/memory add --type T --title .. --content .. [--tags a,b]  新增记忆\n"
        "/memory delete <id>                        删除记忆\n"
        "/memory search <关键词>                     搜索记忆\n"
        "/memory semantic                           查看当前注入的静态层内容\n"
        "  类型 T：semantic(事实偏好) / episodic(情境经历) / procedural(程序经验)")
    reg.register("logs", _cmd_logs,
        "[N]  查看当前 session 日志尾部（默认30行）",
        "/logs           最近 30 行\n"
        "/logs 100       最近 100 行")
    reg.register("download", _cmd_download,
        "[name|list] [dir] [--force]  下载随包资产（工作流/mcp/脚本）",
        "/download list              查看可下载资产\n"
        "/download before_turn_retrieval     下载指定资产到默认目录\n"
        "/download my_workflow .agent/workflows/   下载到指定目录\n"
        "/download my_wf --force     覆盖已有同名文件")
    reg.register("feedback", _cmd_feedback,
        "[类型] <内容>  提交反馈给作者（bug/建议/问题/赞美）",
        "/feedback 工作流编辑器连线不太灵敏        （默认类型=建议）\n"
        "/feedback bug WebUI 切换会话后白屏        （指定类型=bug）")
    reg.register("web", _cmd_web,
        "[start] [port] | stop | status  按需启停内嵌 Web 服务",
        "/web              启动（默认 8000 端口）+ 自动开浏览器\n"
        "/web 9000         指定端口\n"
        "/web stop         停止服务、释放端口\n"
        "/web status       查看服务状态")
    reg.register("snapshot", _cmd_snapshot,
        "list | restore <序号|sha>  工作区快照回溯（检查点）",
        "/snapshot list          列出所有快照点\n"
        "/snapshot restore 3     回溯到第 3 个快照之前（撤销该轮及之后的文件改动+对话）\n"
        "/snapshot restore a1b2  用 sha 前缀回溯")
    reg.register("rewind", _cmd_rewind,
        "[count]  回溯到 count 个 turn 之前（撤销最近 count 轮，默认1）",
        "/rewind            撤销最近 1 轮（对话+文件改动）\n"
        "/rewind 3          撤销最近 3 轮")
    reg.register("rag", _cmd_rag,
        "build | config [k v] | stats | query <词>  RAG 文档库管理",
        "/rag stats                         查看索引状态\n"
        "/rag config                        查看当前配置\n"
        "/rag config docs_dir ./docs        设置文档目录\n"
        "/rag config embed_model_path ./m3e 设置 embedding 模型路径\n"
        "/rag build                         建库（同步执行，打印进度）\n"
        "/rag query 认证模块                 查询测试")
    reg.register("tools", _cmd_tools,
        "[关键词]  列出所有工具（按来源分组：内置/MCP/工作流/LSP）",
        "/tools              列出全部\n"
        "/tools edit         过滤含 'edit' 的工具\n"
        "/tools mcp          过滤 MCP 工具")
    reg.register("call", _cmd_call,
        '[yes] tool_name({"arg":"val"})  手动调用工具',
        '/call read_file({"path":"README.md"})             纯调用（仅显示结果）\n'
        '/call yes grep({"pattern":"TODO","path":"."})      结果注入 Agent 上下文\n'
        '  yes = 工具结果以 user 消息注入（模型下一步能看到）\n'
        '  不加 yes = 结果仅显示，不影响 Agent')
    reg.register("update", _cmd_update,
        "检查并升级到 PyPI 最新版（editable/本地安装自动跳过）")
    reg.register("reload", _cmd_reload,
        "models|tools  热重载：模型配置(models.json) / 脚本工具(tools/*.py)改后免重启生效",
        "/reload models\n"
        "  改 models.json / 修 model id / 换 token 后执行；主 llm 同名刷新 + utility 通道重建\n"
        "/reload tools\n"
        "  改 tools/ 或 .agent/tools/ 的 agt_register 脚本后执行；重扫 + 摘旧挂新，秒级生效")
    reg.register("restart", _cmd_restart,
        "[消息]  看门狗式重启：退出→自动重启→恢复session/端口→推送消息（改完源码生效用）",
        "/restart                     重启并恢复当前会话\n"
        "/restart 继续刚才的任务       重启恢复后自动发送该消息\n"
        "  改了 agt 源码(src/)后用它让新代码生效；日志 ~/.agt/restart.log")
    reg.register("debug", _cmd_debug,
        "prompt <提示词>  调试用：按当前上下文投影直接调 LLM，不落盘不执行，打印完整回包",
        "/debug prompt 你好，介绍下你自己\n"
        "  投影=当前 session 状态+提示词；tool_calls 只展示不执行；\n"
        "  session/events 零写入（llm_calls.jsonl 仍记录，/stats 可观测）")
    reg.register("status", _cmd_status,
        "实例状态总览：模型/队列/子进程树(PID+命令行)/后台服务/团队/钩子（诊断卡死首选）",
        "/status            全量状态；busy 长时间无进展时看进程树——\n"
        "                    run_shell 启动的卡住进程一目了然，taskkill <pid> 解围")
    reg.register("context", _cmd_context,
        "上下文堆积情况：最近react实际tokens + 投影各段(system/折叠/各档/当前/tail)估算占比",
        "/context\n"
        "  长会话想知道「上下文都被什么吃了」时用；段落占比直接对应\n"
        "  messages_for_llm 装配顺序，配合 /stats 折线看缓存命中。")
    reg.register("update-assets", _cmd_update_assets,
        "[apply] [--force]  随包播种资产对比/热更新（pip 升级后把旧工作流/声明刷成新版）",
        "/update-assets           预览：哪些资产落后/本地已改/缺失（不动文件）\n"
        "/update-assets apply     执行安全更新（装缺 + 更新本地未改过的；已改过的跳过保护）\n"
        "/update-assets apply --force   连本地已改/未知状态的也覆盖（基线刷新，慎用）\n"
        "  判定：三方 hash（随包/本地/播种基线 seed_state.json）——本地没改过的才自动更新；\n"
        "  更新即生效（工作流每轮重扫/声明每次读取），无需 /restart。通常配合 /update 升级后使用。")
    reg.register("agent", _cmd_agent,
        "[agent_id]  列出/切换直接交互的 Agent 目标",
        "/agent              列出所有团队成员（agent_id/名称/状态/recap）\n"
        "/agent coder        切换到与 coder 直接交互\n"
        "/agent _main_       切回主 Agent\n"
        "  仅允许 done/idle 状态的子 Agent；running 的需等完成后才能切换")
    reg.register("exit", _cmd_exit,
        "优雅退出（= /quit = 裸词 quit）：等当前任务完成后退出；agt-web 模式会关掉整个服务",
        "/exit              退出（所有入口可用：CLI/WebUI/stdin 驱动）\n"
        "/quit              同上（别名）")
    reg.register("quit", _cmd_exit, "同 /exit（别名）")
    # /help 需要访问 reg 自身，单独绑
    reg.register("help", lambda ctx, args: reg.print_help(), "显示本帮助")
    return reg
