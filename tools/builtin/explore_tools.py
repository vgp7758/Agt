"""explore_tools.py —— 外置探索工具（spec s_54a1eb86：工作流式 react 探索 + Step 嫁接）。

用户提案 2026-09-09：纯探索过程（多轮 grep/read_file 定位代码）对历史会话依赖低——
由本工具在小上下文 react 循环里完成，工具调用过程嫁接回主 agent steps（复用
agent._seed_steps：toollog.record + Step + add_step——events.jsonl/读档重放/步距
衰减全走既有管线，零新落盘代码），主 agent 只拿结构化摘要继续干活。

只读白名单：探索 agent 不能改文件——改动决策留给主 agent。
recent-file 语义澄清：_FILE_SNAP_TOOLS 只挂写工具（edit/write…），探索全只读——
嫁接步不进 rf_map 是与主循环一致的正确行为（rf 管"本轮变更文件速览"）。

需要 ctx["agent"]（chat.py → attach_script_tools → scan_script_tools 注入）；
无 agent 引用（纯工具箱构建/测试）时本组工具不注册（降级，不炸主程序）。
改完本文件用 /reload tools 热加载。
"""
from __future__ import annotations

import json
import time

# 只读工具白名单：探索 agent 可用的全部工具（改文件类一律拒绝）
_READ_ONLY = frozenset({"grep", "read_file", "glob_files", "find_function", "list_dir"})

_SYSTEM = (
    "你是代码探索员，在一个代码仓库里定位与目标相关的代码。规则：\n"
    "1. 只能调用给定工具（全部只读）；优先 grep 定位 → read_file/find_function 看实现。\n"
    "2. 每步少读：read_file 用 start_line/end_line 分段，不要整读大文件。\n"
    "3. 信息足够后【立即停止调用工具】，用要点输出总结：相关位置（文件:行号）、关键函数/类、"
    "与目标的关系、值得注意的细节。总结要具体（带行号与函数名），这是调用方唯一确定保留的内容。"
)

_RESULT_CAP = 6000   # 单次工具结果进探索上下文/嫁接记录的字符上限（防投影膨胀）


def _make_explore(agent):
    def explore(goal: str, max_steps: int = 8, budget_seconds: int = 120, model: str = "") -> str:
        """外置探索：把"找相关代码"的多轮 grep/read 过程外包给小上下文 react 循环（默认 utility 模型，
        便宜且不占你的步数），探索的原始工具调用记录自动嫁接进本轮上下文（可追溯），你直接拿摘要继续工作。
        何时用：需要 3 步以上搜索/阅读才能定位的探索（如"找到 X 功能的实现和调用链"）；
        单次 grep 能命中时直接自己调更省。返回=结构化摘要（不衰减）；嫁接步允许轮内衰减。

        goal: 探索目标——尽量具体（要找什么、在哪个模块、关注哪些方面）
        max_steps: 最多工具调用轮数（默认 8，上限 20）
        budget_seconds: 墙钟预算秒（默认 120；超限返回已完成部分）
        model: 探索用模型名（空=utility_model）"""
        max_steps = max(1, min(int(max_steps or 8), 20))
        budget_seconds = max(10, int(budget_seconds or 120))
        try:
            schemas = [s for s in agent._llm_tool_schemas()
                       if (s.get("function") or {}).get("name") in _READ_ONLY]
        except Exception as e:
            return f"[explore] 工具 schema 获取失败：{type(e).__name__}: {e}"
        if not schemas:
            return "[explore] 无可用只读工具（白名单 " + ", ".join(sorted(_READ_ONLY)) + " 均未注册）"

        if model:
            try:
                from llm_client import LLMClient
                llm = LLMClient(model_name=model, enable_thinking=False, max_retries=2)
                llm.call_recorder = agent.session.llm_calls.record
            except Exception as e:
                return f"[explore] 模型 {model} 初始化失败：{e}"
        else:
            llm = agent.utility_client()

        messages = [{"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": f"探索目标：{goal}"}]
        seeds, summary, stop_reason = [], "", "步数上限"
        deadline = time.time() + budget_seconds
        try:
            for _ in range(max_steps):
                if time.time() >= deadline:
                    stop_reason = "时限"
                    break
                resp = llm.chat(messages, tools=schemas, scene="explore")
                tcs = resp.tool_calls or []
                if not tcs:
                    summary = (resp.content or "").strip()
                    stop_reason = "完成"
                    break
                messages.append({
                    "role": "assistant", "content": resp.content or None,
                    "tool_calls": [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"],
                                                 "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                                   for tc in tcs]})
                for tc in tcs:
                    name, args = tc["name"], dict(tc["arguments"] or {})
                    if name not in _READ_ONLY:
                        result = f"[explore 白名单拒绝] {name} 不可用（探索只读）"
                    else:
                        try:
                            result = str(agent._exec_tool(name, args))
                        except Exception as e:
                            result = f"[执行出错] {type(e).__name__}: {e}"
                    if len(result) > _RESULT_CAP:
                        result = result[:_RESULT_CAP] + "\n…[explore 截断]"
                    # reasoning 置空 + 标注来源：DeepSeek requires_reasoning_in_history 占位规则按空值自然处理
                    seeds.append({"tool": name, "args": args, "result": result,
                                  "reasoning": "[外置探索] " + (resp.reasoning or "")[:150]})
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            if not summary:   # 预算耗尽没出总结——无工具快速收口一次（部分结果优于断链）
                try:
                    messages.append({"role": "user",
                                     "content": "预算已尽。立即停止调用工具，用要点总结已获得的信息（可含'未查完'部分）。"})
                    r2 = llm.chat(messages, tools=None, scene="explore")
                    summary = (r2.content or "").strip()
                except Exception:
                    pass
        except Exception as e:
            stop_reason = f"异常:{type(e).__name__}"

        grafted = 0
        if seeds:
            try:
                agent._seed_steps(seeds)   # toollog + Step + add_step（自动落 events.jsonl）；此时 explore 调用步尚未归档 → 嫁接步自然在前
                grafted = len(seeds)
            except Exception as e:
                grafted = f"失败:{type(e).__name__}"

        head = (f"[外置探索·{stop_reason}] 嫁接 {grafted} 步工具调用进本轮上下文"
                f"（原始记录可追溯，随轮内逐步衰减）。\n要点摘要：\n")
        return head + (summary or "（无摘要——预算内未产出结论，建议自行降级手工探索）")
    return explore


def agt_register(ctx=None):
    """ctx: {"cwd", "agent", ...}——agent 由 chat.py 注册链注入；缺失时不注册（纯工具箱场景降级）。"""
    agent = (ctx or {}).get("agent")
    if agent is None:
        return []
    return [{
        "name": "explore", "func": _make_explore(agent),
        "group": "搜索定位", "version": 1,
        "params": {
            "goal": "探索目标（要找什么代码/信息——尽量具体：模块/关键词/关注点）",
            "max_steps": "最多工具调用轮数（默认 8）",
            "budget_seconds": "墙钟预算秒（默认 120）",
            "model": "探索用模型名（空=utility_model）",
        },
    }]
