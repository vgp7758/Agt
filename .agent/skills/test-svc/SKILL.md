---
name: test-svc
description: 技能服务协议演示（skill_equip/skill_use 全链路测试用，可删除）
when_to_use: 测试技能服务装备/使用协议时
---

# test-svc（测试技能）

验证 skill_equip / skill_use 协议的演示技能。包内 server.py 实现计数器服务：

```
add N:        # 计数器加 N（N=整数）
show:         # 显示当前计数
echo MSG...:  # 原样返回后面的文本
```

用法：`skill_equip("test-svc")` → `skill_use("test-svc", "add 5")`。
