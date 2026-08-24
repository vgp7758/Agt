"""rag_tools.py —— rag_query 外置件（真限界上下文：向量库 .db/index 由 rag 组自写自读）。

外置判别标准（wiki: architecture/tool-externalization-criteria.md）：
faiss 向量索引 + chunks.db 由 RAG 自己建自己查——数据主权在 rag 组，可外置。
工具函数本体仍住在框架 rag.py（与 cosine_sim/session_vec 共享单例 embedder——
外置的是【注册】与【预热触发】，不是复制实现）：

- import rag 在主进程零成本（chat.py 已 import，sys.modules 直接命中）
- agt_register 时 preload_async 后台预热模型：SentenceTransformer 加载秒级~十秒级，
  不阻塞启动/装配线程（build_agent 里的 /reload tools 重扫也不卡）
- rag_query 自带惰性 ensure：预热未完成时调用同步等锁拿结果

改完本文件用 /reload tools 热加载（不需要重启）。
"""


def agt_register():
    import rag
    rag.preload_async()   # 注册即异步预热（幂等：ensure 内部锁 + attempted 标志）
    return [{"name": "rag_query", "func": rag.rag_query,
             "group": "rag", "version": 1}]
