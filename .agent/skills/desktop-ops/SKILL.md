---
name: desktop-ops
description: 桌面键鼠操作能力——鼠标点击/移动/拖拽、键盘按键/输入/组合键、剪切板读写、窗口列表/切换、画面帧截取（pyautogui + pygetwindow）
when_to_use: 需要操作本机 GUI（点击/输入/切窗/自动化桌面流程）的 Agent 装备；配合画面帧注入（image_feed）实现"看着屏幕操作"
assemblies:
  - position: after:steps
    ass:
      - image_feed: {tool: 'skill_command(name="desktop-ops", command="screen_capture")'}
---

# 桌面键鼠操作 SOP

本技能能力全量走 **stdin 服务**（server.py 行协议，2026-10-17 用户裁定：避免装配为工具）——
统一用 `skill_use("desktop-ops", "<命令>")` 调用，首次使用自动装备拉起服务。

命令表（CLI 风格，`--k v` 传参；文本含空格用引号）：

```
skill_use("desktop-ops", "click --x 500 --y 300")                 # 点击（可加 --button right --double True）
skill_use("desktop-ops", "move --x 500 --y 300")                  # 移动（悬停）
skill_use("desktop-ops", "drag --x1 10 --y1 10 --x2 200 --y2 200")# 拖拽（可加 --duration 0.4）
skill_use("desktop-ops", "key enter")                             # 单键（--presses 3 连按）
skill_use("desktop-ops", 'type "hello world"')                    # 输入文本（中文自动走剪切板）
skill_use("desktop-ops", "hotkey ctrl c")                         # 组合键（键名序列）
skill_use("desktop-ops", "clip_r")                                # 读剪切板
skill_use("desktop-ops", 'clip_w --text "一段文字"')               # 写剪切板
skill_use("desktop-ops", "wins")                                  # 窗口清单（标题|位置|前台标记）
skill_use("desktop-ops", "focus --title 记事本")                   # 按标题模糊切窗
```

画面帧由命令口提供：`screen_capture`（截屏存盘返回路径）——本技能 `assemblies:` 已声明
image_feed 帧源走它，装备时自动往宿主 `after:steps` 插画面帧段——视觉宿主每步看到最新画面，
无需宿主 yml 手写 image_feed 段。也可 `skill_command("desktop-ops", "screen_capture")` 手动取帧。

操作对象是本机真实 GUI——键鼠动作会影响真实桌面。

## 坐标规范（重要）

画面帧被等比缩放到宽 1280——你在截图/画面帧里看到的 (x, y) 就是命令要的坐标（画面像素）。
click/move/drag 内部自动换算到真实屏幕（含 DPI 缩放 125%/150% 场景，换算系数 k=屏幕宽/1280，
y 同系数），你只管报画面坐标，不要自己乘缩放比。

## 工作循环

```
看（画面帧）→ 定位（坐标）→ 操作（skill_use 键鼠命令）→ 验证（下一步的帧）
```

每一步操作后，下一步的投影会带最新画面——**必须看新帧确认操作效果**，不要盲打。

## 操作纪律

1. **看后操作**：任何键鼠动作前，确认目标在当前画面里可见；不可见先 `focus`（切窗）/ `hotkey win d`（回桌面）/ `hotkey alt tab`。
2. **操作后验证**：点击后看下一帧——生效了才继续；没生效换策略（坐标偏了/加载慢）。
3. **等待加载**：操作后画面未变，等 1~2 秒再看；窗口切稳后即可操作。
4. **文本输入**：中文/复杂文本优先 `clip_w --text ...` + `hotkey ctrl v`（绕开输入法拦截）；纯 ASCII 可直接 `type`。
5. **窗口定位优先 `focus --title`**（按标题模糊匹配），比点任务栏稳。
6. **失败降级**：坐标点击无效 2 次换 keyboard 路线（tab 导航 + enter）；窗口找不到用 `wins` 核对标题。
