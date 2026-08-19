# get_list_item · 列表元素取值工具（LIGHT_TOOLS）

> 源码：`src/real_tools.py`（注册于 `LIGHT_TOOLS`）
> 职责：从列表/数组取单个元素，支持正/负索引、dict 元素访问，越界返回错误提示。2026-08 落地（commit 9fb00de）。

## 职责与用法

`get_list_item(lst, index)`：

- **lst**：列表/数组（通常接上游节点输出，如 `all_outputs`）
- **index**：0-based 索引；`-1` 为末元素；负索引倒序访问
- **典型用途**：从 `all_outputs` / 上游 list 输出取单个元素，比 selector 运算符直接

**返回类型**：
- outputs=**any**（编辑器可改类型）——类型随元素；越界/None 返回错误提示文本（`[越界] index=5，列表长度 3`），不炸工作流
- dict 输入原样返回（兼容 selector 运算符）

## 特性

### 正/负索引支持

```python
get_list_item([1, 2, 3], 0)   # → 1
get_list_item([1, 2, 3], -1)  # → 3
get_list_item([1, 2, 3], -2)  # → 2
```

### dict 元素访问

```python
get_list_item({"a": 1, "b": 2}, "a")  # → 1
```

### 越界安全

```python
get_list_item([1, 2, 3], 5)   # → "[越界] index=5，列表长度 3"
```

## LIGHT_TOOLS 与 any 类型

- 注册于 `LIGHT_TOOLS`，`outputs=[{"name": "raw", "type": "any", "description": "列表元素（类型随元素；越界返回错误文本）"}]`
- **any 类型不锁 schema**——编辑器可改 object 逐字段连线组装结构透传（见 [workflow-hooks pass_through 工具](../architecture/workflow-hooks.md#pass_through-工具light_toolsinputany-schema-空-编辑器-any-类型不锁可改-object-逐字段连线组装结构透传)）

## 与 selector 运算符的关系

| 方式 | 用途 |
|------|------|
| selector 运算符 | 简单取值，LLM 直接写 |
| **get_list_item 工具** | 工作流节点间连线取值，支持动态索引、越界安全 |

## 注意事项

- 工具注册后需 `/restart` 才在当前进程工具箱可见
- 越界返回错误提示文本而非抛异常——工作流不炸，但需消费端判断是否为错误提示

## 相关页面

- [工作流引擎与钩子](../architecture/workflow-hooks.md)：LIGHT_TOOLS / any 类型 / plugin 节点
- [系统总览](../architecture/overview.md)：能力层 real_tools.py