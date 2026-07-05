---
name: analyze-replay
description: 分析 AgenTank 对战回放，定位输球原因
when_to_use: 输了一场、需要诊断战术失误或代码 bug 时
---

# 分析对战回放 SOP

1. 先用 `__mcp__agentank__get_match_analysis`（match_url_id）拉 **compact** 视图，看胜负、reason、双方关键统计。
2. 重点判读 `reason`：
   - `crashed` → 正常战损，看走位/瞄准/躲弹/抢星/技能时机；
   - `runtime` → 代码超时，简化循环/寻路/日志；
   - `error` → 代码异常，查 null 检查、坐标处理、函数调用。
3. 若 compact 不够，加 `view=events` 看关键事件流（开火/转向/吃星/技能），**不要**轻易用 `raw`（很费 token）。
4. 需要精确时机（某几帧的躲弹/瞄准）再用 frame 切片。
5. 给出**最关键的一两条**改进点（不要罗列一堆），优先修最可能扭转胜负的。
6. 改完先 simulate 验证，再让用户决定是否 publish。
