---
name: desktop-ops
description: 桌面键鼠操作能力——鼠标点击/移动/拖拽、键盘按键/输入/组合键、剪切板读写、窗口列表/切换、画面帧截取（pyautogui + pygetwindow）
when_to_use: 需要操作本机 GUI（点击/输入/切窗/自动化桌面流程）的 Agent 装备；配合画面帧注入（image_feed）实现"看着屏幕操作"
assemblies:
  - position: after:steps
    ass:
      - image_feed: {tool: screen_frame()}
---

# 桌面键鼠操作 SOP

本技能提供 11 个外置工具：`mouse_click` / `mouse_move` / `mouse_drag` / `key_press` / `key_type` / `key_hotkey` / `clipboard_read` / `clipboard_write` / `window_list` / `window_focus` / `screen_frame`。操作对象是本机真实 GUI——键鼠动作会影响真实桌面。

本技能自带装配清单（SKILL.md `assemblies:`）：装备时自动往宿主装配清单 `after:steps` 插入画面帧段（`image_feed: {tool: screen_frame()}`，帧提供者抽象）——视觉宿主每步自动看到最新画面（装备时冻结快照），无需宿主 yml 手写 image_feed 段或拉起画面服务。

## 坐标规范（重要）

画面帧被等比缩放到宽 1280——你在截图/画面帧里看到的 (x, y) 就是工具要的坐标（画面像素）。
`mouse_*` 工具内部自动换算到真实屏幕（含 DPI 缩放 125%/150% 场景，换算系数 k=屏幕宽/1280，y 同系数），你只管报画面坐标，不要自己乘缩放比。

## 工作循环

```
看（画面帧）→ 定位（坐标）→ 操作（键鼠）→ 验证（下一步的帧）
```

每一步操作后，下一步的投影会带最新画面——**必须看新帧确认操作效果**，不要盲打。

## 操作纪律

1. **看后操作**：任何键鼠动作前，确认目标在当前画面里可见；不可见先 `window_focus` / `key_hotkey("win","d")` / `key_hotkey("alt","tab")`。
2. **操作后验证**：点击后看下一帧——生效了才继续；没生效换策略（坐标偏了/加载慢）。
3. **等待加载**：操作后画面未变，等 1~2 秒再看；窗口切稳后（`window_focus` 内置 0.3s）即可操作。
4. **文本输入**：中文/复杂文本优先 `clipboard_write` + `key_hotkey("ctrl","v")`（绕开输入法拦截）；纯 ASCII 可直接 `key_type`。
5. **窗口定位优先 `window_focus`**（按标题模糊匹配），比点任务栏稳。
6. **失败降级**：坐标点击无效 2 次换 keyboard 路线（tab 导航 + enter）；窗口找不到用 `window_list` 核对标题。
