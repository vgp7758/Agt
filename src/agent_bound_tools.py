"""agent_bound_tools —— 有状态工具族按 Agent 实例重建（p_c0eebb19 步 2，2026-11-27）。

背景（20048 实锤）：子 Agent 的工具箱直接复用 main.tools 里同一批 Tool 对象——
make_plan_tools / make_spec_tools / make_recall_tools / make_session_tools /
make_hook_side_effects 这几族工厂闭包里的 agent 恒为 main。于是 worker 的
update_plan 改的是 main 的计划、hook_write 写的是 main 的 recap。

本模块做两件事：
· 单一事实源：AGENT_BOUND 工厂表（组名 → 工厂）——chat.build_agent 装配 main 时
  也由这张表驱动注册（不再各写一份）；组名与 chat.py 的 _reg(..., 组名) 一致。
· rebind_agent_tools(sub, main)：对 sub.tools 中属于 AGENT_BOUND_GROUPS 组的工具，
  用同名工厂以 sub 为 agent 重建后原地替换（保持 Toolbox 对象身份不变）。
  组名拿不到时按名字集合兜底（名字集合 = 以 main 为 agent 跑一遍工厂表）。

「用户交互」（ask_user/human_step 走 main 的 UI 通道）、「后台/调度」「自定义端点」
「子Agent」是实例级设计，不重绑。
"""
import logging

_LOG = logging.getLogger("agt.agent_bound_tools")

# 有状态工具族组名（= chat.build_agent 里 _reg 的组名）。这些组的工具闭包绑 agent 实例，
# 子 Agent 必须重建自己的份。
AGENT_BOUND_GROUPS = ("计划", "施工方案", "记忆召回", "会话", "钩子")


def agent_bound_factories() -> list:
    """AGENT_BOUND 工厂表：[(组名, 工厂), ...]。工厂统一签名 (agent) -> list[Tool]。
    函数内延迟 import：multiagent ↔ chat 循环 import（make_hook_side_effects 在
    multiagent.py 里），表在本模块只在调用时才解引用。"""
    from plan_tools import make_plan_tools
    from spec_tools import make_spec_tools
    from memory_tools import make_recall_tools
    from session_tools import make_session_tools

    def _make_hook(agent):   # 延迟到调用时刻 import（防御循环 import 下的加载顺序）
        from multiagent import make_hook_side_effects
        return make_hook_side_effects(agent)

    return [
        ("计划", make_plan_tools),
        ("施工方案", make_spec_tools),
        ("记忆召回", make_recall_tools),
        ("会话", make_session_tools),
        ("钩子", _make_hook),
    ]


def register_agent_bound_groups(agent, reg) -> None:
    """chat.build_agent 装配入口：按工厂表把五组工具经 reg(tools, 组名) 注册进 main。
    reg 是 build_agent 的 _reg 闭包（注册 + tool_groups 标注）。"""
    for group, factory in agent_bound_factories():
        try:
            reg(factory(agent), group)
        except Exception as e:
            _LOG.warning("装配 agent-bound 工具组 '%s' 失败（跳过）：%s", group, e)


def rebind_agent_tools(sub, main) -> list:
    """把 sub.tools 中 agent 闭包绑在 main 上的有状态工具按 sub 重建并原地替换。

    · 判定：工具名在 main.tool_groups 里标了 AGENT_BOUND_GROUPS 组名；或（兜底，
      tool_groups 丢失时）名字出现在「以 main 跑一遍工厂表」产出的名字集合里。
    · 替换：直接写 sub.tools._tools[name]（保持 Toolbox 对象身份——SubAgent 已把它
      传进 session/装配各处，换箱子会丢引用）；hidden 标志随旧工具保留。
    · 返回原地替换的工具名列表（日志/测试断言用）。
    """
    factories = agent_bound_factories()

    # main 视角：name -> factory（组名 + 名字集合双通道都指向这张表）
    main_bound = {}   # 工具名 -> (组名, 工厂)
    for group, factory in factories:
        try:
            for t in factory(main):
                main_bound.setdefault(t.name, (group, factory))
        except Exception as e:
            _LOG.warning("工厂 %s 以 main 试跑失败（跳过）：%s", getattr(factory, "__name__", factory), e)
    main_groups = getattr(main, "tool_groups", {}) or {}
    main_tools = getattr(getattr(main, "tools", None), "_tools", {}) or {}
    _built = {}      # id(factory) -> {工具名: Tool}：以 sub 为 agent 每族只重建一次

    replaced = []
    for t in list(sub.tools):
        # 只重绑「借来的 main 同一对象」（复核补强）：子 Agent 专属工具 / 技能工具若与某计划工具
        # 同名（_agent_own_tools / sk_tools 覆盖注册），它们不是借来的——不动，保住专属实现。
        if main_tools.get(t.name) is not t:
            continue
        hit = main_bound.get(t.name)
        if hit is None:
            if main_groups.get(t.name) in AGENT_BOUND_GROUPS:
                # 组名说该重绑，但工厂表以 main 跑不出这个名字（如 main 未装配该组）
                # → 不动它：重建无从谈起，保留现状比丢失工具安全。
                _LOG.debug("工具 '%s' 组 '%s' 属 agent-bound 但工厂表无产出，保留原样",
                           t.name, main_groups.get(t.name))
            continue
        _group, factory = hit
        fam = _built.get(id(factory))
        if fam is None:
            try:
                fam = {x.name: x for x in factory(sub)}
            except Exception as e:
                _LOG.warning("工具族 %s 按子 Agent 重建失败（保留 main 版）：%s",
                             getattr(factory, "__name__", factory), e)
                fam = {}
            _built[id(factory)] = fam
        new_tool = fam.get(t.name)
        if new_tool is None:
            continue
        new_tool.hidden = getattr(t, "hidden", False)   # hidden 标志（如 get_session_history）随旧工具
        sub.tools._tools[t.name] = new_tool
        replaced.append(t.name)
    if replaced:
        _LOG.info("子 Agent '%s' 重绑 %d 个 agent-bound 工具：%s",
                  getattr(sub, "agent_id", "?"), len(replaced), ",".join(replaced))
    return replaced
