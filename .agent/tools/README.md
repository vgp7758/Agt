# .agent/tools/ — 私有脚本工具目录（workspace 级覆盖层）

与 `tools/builtin/` 同一套 `agt_register` 约定，但定位是**私有/实验层**：
扫描顺序为 随包 builtin → `tools/builtin/` → `.agent/tools/`，同名后注册胜出——
放这里可以**覆盖**前两层同名工具，适合试验新版实现而不动正式文件。

## 约定

- 脚本会被 import 执行：不要放一次性任务脚本（同 `tools/builtin/` 的预检防御）。
- 实验稳定后建议挪去 `tools/builtin/` 转正。
- `/reload tools` 热生效。
