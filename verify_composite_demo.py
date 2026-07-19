"""验证 composite_demo.xml：循环+批处理+单节点批处理 三合一工作流端到端跑通"""
import sys, os
sys.path.insert(0, "src")
sys.stdout.reconfigure(encoding="utf-8")

from types import SimpleNamespace
import workflow
from workflow_xml import xml_to_canvas


class MockLLM:
    def chat(self, msgs, **kw):
        # 演示目的：直接返回评语文本
        return SimpleNamespace(content="表现不错")


class MockTools:
    def __contains__(self, name):
        return name in ("multiply", "to_uppercase", "add")

    def call(self, name, args):
        if name == "multiply":
            return float(args.get("a", 0)) * float(args.get("b", 1))
        if name == "to_uppercase":
            return str(args.get("text", "")).upper()
        if name == "add":
            return float(args.get("a", 0)) + float(args.get("b", 0))
        raise workflow.WorkflowError(f"未知工具 {name}")


def run(canvas, inputs):
    return workflow.execute(canvas, inputs,
                            tools=MockTools(), llm=MockLLM(),
                            return_exit_dict=True)


print("=== 加载 composite_demo.xml ===")
canvas = xml_to_canvas(open(".agent/workflows/composite_demo.xml", encoding="utf-8").read())
print(f"解析成功: {len(canvas['nodes'])} 节点")

# 输入数据
inputs = {
    "items": [
        {"name": "Alice", "score": 90},
        {"name": "Bob",   "score": 60},
        {"name": "Charlie", "score": 75},
    ],
    "nums": [10, 20, 30, 40, 50],
}

print("=== 执行工作流 ===")
result = run(canvas, inputs)
print(f"完成。返回: {list(result.keys())}")

# ---- 校验 ----
errors = []

# 1. LOOP 输出：每轮含 #index + 大写名 + 最终得分，累加到本地变量 report
report = result.get("loop_report", "")
print(f"\n[1] 循环 report: '{report}'")
# 预期: "#0 ALICE:540.0;#1 BOB:360.0;#2 CHARLIE:450.0;"
#   Alice(90): boosted=180, *3=540.0 → "#0 ALICE:540.0;"
#   Bob(60):   boosted=120, *3=360.0 → "#1 BOB:360.0;"
#   Charlie(75): boosted=150, *3=450.0 → "#2 CHARLIE:450.0;"
checks = [
    ("report 含 #0 (第0轮)", "#0" in report),
    ("report 含 #1 (第1轮)", "#1" in report),
    ("report 含 #2 (第2轮)", "#2" in report),
    ("report 含 ALICE:540", "ALICE:540" in report),
    ("report 含 BOB:360", "BOB:360" in report),
    ("report 含 CHARLIE:450", "CHARLIE:450" in report),
    ("三轮以分号分隔", report.count(";") >= 3),
]
for label, ok in checks:
    s = "  ✅" if ok else "  ❌"
    print(f"{s} {label}")
    if not ok: errors.append(label)

# 2. BATCH 输出：continue 捕获本轮输出 → all_outputs/nth_output
batch_all = result.get("batch_all", [])
batch_nth = result.get("batch_nth") or {}
print(f"\n[2] 批处理 all_outputs ({len(batch_all)} 轮):")
for i, item in enumerate(batch_all):
    print(f"     [{i}] name={item.get('name')}, comment={item.get('comment')}")
print(f"     nth: {batch_nth}")
b_checks = [
    ("all_outputs 3 轮", len(batch_all) == 3),
    ("all[0] 含 name=Alice", batch_all[0].get("name") == "Alice" if batch_all else False),
    ("all[0] 含 comment", "comment" in (batch_all[0] or {}) if batch_all else False),
    ("nth_output = all[0]", batch_nth.get("name") == "Alice"),
]
for label, ok in b_checks:
    s = "  ✅" if ok else "  ❌"
    print(f"{s} {label}")
    if not ok: errors.append(label)

# 3. 单节点批处理：nth_output = 第二个 filtered（≥50 筛选后第 2 个）
nth = result.get("single_nth") or {}
print(f"\n[3] 单节点批处理 nth: {nth}")
# nums=[10,20,30,40,50], multiply*2: [20,40,60,80,100]
# filter raw>50: [60,80,100]
# nth=1（第二个）→ 80
raw_val = nth.get("raw")
if raw_val == 80:
    print("  ✅ nth_output.raw == 80（60/80/100 第二个=80）")
else:
    print(f"  ❌ 期望 raw=80，实际={raw_val}")
    errors.append("single_nth")

# ---- 汇总 ----
print(f"\n{'='*40}")
if errors:
    print(f"❌ 失败 {len(errors)} 项：{errors}")
    sys.exit(1)
else:
    print("✅ 三合一工作流 composite_demo.xml 全部跑通！")
