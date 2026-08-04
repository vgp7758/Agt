"""verify_md_render.py —— 肉眼检查 mdrender 的 CLI 渲染（表格对齐 / 代码块 / inline code）。

跑法：python verify_md_render.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from mdrender import render_cli, ascii_table, disp_width

SAMPLE = r'''这是一段普通说明，里面有 `inline code` 和 **不会渲染的加粗**（加粗不在范围内）。

下面是一个中英混排表格（含 ✨ emoji 和全角标点 ……——）：

| 模型 | 提供方 | 上下文 | 是否推理 |
| :--- | --- | ---: | :---: |
| GLM-4.6 | 智谱✨ | 200K | 是 |
| Qwen3-235B | 阿里 | 128K | 否 |
| GPT-5 | OpenAI | 400K | 是 |

再来一段代码：

```python
def hello(name):
    print(f"hi, {name}")
```

表格里带转义竖线 \| 与 inline code 的情形：

| 命令 | 含义 |
| --- | --- |
| `ls \| grep x` | 管道示例 |
| `\|` | 字面竖线 |

列数不齐（最后一行少一格，应按表头归一不报错）：

| A | B | C |
| --- | --- | --- |
| 1 | 2 | 3 |
| 4 | 5 |

只有表头 + 分隔行的空表：

| 仅表头 | 第二列 |
| --- | --- |

代码块里包含 `| a | b |` 这样的竖线，不应被当表格：

```
伪表格 | 看看 |
| 会不会 | 错认 |
```

收尾普通文本 `tail code`。'''


def main():
    print("=" * 64)
    print("disp_width 自检（中文2 / ascii1 / 全角标点2 / emoji VS 不多算）：")
    for s in ["中文", "abc", "……——", "“”‘’", "·", "✏️", "🤖", "✨"]:
        print(f"  {s!r:14} → {disp_width(s)}")
    print("=" * 64)
    print("【整段 render_cli 渲染】\n")
    print(render_cli(SAMPLE))
    print("\n" + "=" * 64)
    print("【直接 ascii_table：纯英文短表】\n")
    print(ascii_table(["name", "age"], [["Ada", "36"], ["Alan", "41"]]))
    print("\n" + "=" * 64)
    print("若上面所有表格列竖线对齐、中文列不歪、代码块带 │ 前缀 → 通过。")


if __name__ == "__main__":
    main()
