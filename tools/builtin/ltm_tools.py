"""ltm_tools.py —— 长期记忆五件套外置件（真限界上下文：memories/*.jsonl 自写自读）。

外置判别标准（wiki: architecture/tool-externalization-criteria.md）：
memories/*.jsonl 由 add/update/delete_memory 自己写自己读——数据主权在工具组。
工具函数本体住在框架 longterm_memory.py（经 ensure_ltm 模块级单例与 Agent 的注入
provider 共享同一实例——内存缓存不分裂：工具写一条 provider 立即可见）；
本外置件只做注册（rag 模式）。origin_session 元数据由 provider 每轮刷新到单例的
_origin_session 字段（_ltm_static_block 顺带更新，add_memory 读取）。

改完本文件用 /reload tools 热加载（不需要重启）。
"""


def agt_register():
    import longterm_memory as lm
    return [
        {"name": "add_memory", "func": lm.add_memory, "group": "长期记忆", "version": 1},
        {"name": "search_memory", "func": lm.search_memory, "group": "长期记忆", "version": 1},
        {"name": "read_procedure", "func": lm.read_procedure, "group": "长期记忆", "version": 1},
        {"name": "update_memory", "func": lm.update_memory, "group": "长期记忆", "version": 1},
        {"name": "delete_memory", "func": lm.delete_memory, "group": "长期记忆", "version": 1},
    ]
