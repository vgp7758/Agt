"""本地 RAG：faiss(HNSW 向量检索) + sqlite(片段元数据) + 本地 embedding 模型。
切片保留 (file_path, start_line, end_line)，查询返回带行号的 top-K，可选 reranker 精排。

HNSW 图索引让检索 O(logN)，毫秒出 top-K，不是串行遍历——海量数据查询快就靠这个。

配置驱动：<workspace>/.agent/rag.json（见 config.load_rag_config / DEFAULT_RAG_CONFIG）。
- 命令行 demo：python src/rag.py
- agt 集成：web.py 启动时 LocalRAG.from_config(ws) 建全局单例，set_rag() 注入；
  rag_query 工具供智能体调用，/rag 页面供用户管理（配置/建库/查询）。
"""
import fnmatch
import sqlite3
import threading
import time
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer, CrossEncoder


class APIEmbedder:
    """OpenAI 兼容的 /v1/embeddings API embedder（硅基流动/智谱/OpenAI 等大多兼容）。
    对齐 SentenceTransformer 接口：encode(texts,...)→np.ndarray；get_sentence_embedding_dimension()。"""
    def __init__(self, base_url, api_token, model, dim=0):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        self.model = model
        self.dim = dim   # 0 = 第一次 encode 时自动探测

    def get_sentence_embedding_dimension(self):
        return self.dim

    def encode(self, texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False):
        import httpx
        out = []
        for i in range(0, len(texts), batch_size):
            batch = list(texts[i:i + batch_size])
            r = httpx.post(self.url, json={"model": self.model, "input": batch},
                           headers=self.headers, timeout=300)
            r.raise_for_status()
            data = r.json().get("data", [])
            out.extend(d["embedding"] for d in sorted(data, key=lambda x: x.get("index", 0)))
        vecs = np.asarray(out, dtype="float32")
        if self.dim == 0 and vecs.size:
            self.dim = vecs.shape[1]
        if normalize_embeddings and vecs.size:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.maximum(norms, 1e-12)
        return vecs


class LocalRAG:
    def __init__(self, embedder, index_dir, reranker_path=None, config=None):
        self.embedder = embedder
        self.dim = embedder.get_sentence_embedding_dimension()
        print(f"[rag] embedder={type(embedder).__name__} dim={self.dim}")
        self.store_dir = Path(index_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.faiss_path = self.store_dir / "vecs.index"
        self.db_path = self.store_dir / "chunks.db"
        self.config = config or {}
        self.reranker = None
        if reranker_path and Path(reranker_path).exists():
            print(f"[rag] 加载 reranker {reranker_path} ...")
            self.reranker = CrossEncoder(reranker_path)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_db()
        self.index = self._new_index()
        self._next_id = 0
        # 加载已有索引（维度匹配则复用，重启后查询不丢）
        if self.faiss_path.exists():
            try:
                loaded = faiss.read_index(str(self.faiss_path))
                if loaded.d == self.dim:
                    self.index = loaded
                    self._next_id = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                    print(f"[rag] 已加载现有索引：{self.index.ntotal} 向量")
            except Exception as e:
                print(f"[rag] 加载现有索引失败（将用空索引）：{e}")

    @classmethod
    def from_config(cls, workspace, cfg=None):
        """按 rag.json 构建实例；enabled 关或 embedder 配置不全 → 返回 None（不抛）。"""
        if cfg is None:
            from config import load_rag_config
            cfg = load_rag_config(workspace)
        if not cfg.get("enabled"):
            return None
        # embedder：local（SentenceTransformer）或 api（APIEmbedder，OpenAI 兼容）
        provider = cfg.get("embed_provider", "local")
        try:
            if provider == "api":
                if not cfg.get("embed_api_url") or not cfg.get("embed_api_model"):
                    print("[rag] API embedding 缺 embed_api_url/embed_api_model")
                    return None
                embedder = APIEmbedder(cfg["embed_api_url"], cfg.get("embed_api_token", ""),
                                        cfg["embed_api_model"], cfg.get("embed_api_dim", 0))
            else:
                if not cfg.get("embed_model_path"):
                    return None
                print(f"[rag] 加载本地 embedding 模型 {cfg['embed_model_path']} ...")
                embedder = SentenceTransformer(cfg["embed_model_path"])
        except Exception as e:
            print(f"[rag] embedder 初始化失败：{e}")
            return None
        # index_dir：空/旧默认 → per-repo 用户目录
        index_dir_cfg = cfg.get("index_dir", "")
        if not index_dir_cfg or index_dir_cfg == ".agent/rag":
            from session import REPOS_DIR, _repo_hash
            index_dir = REPOS_DIR / _repo_hash(workspace) / "rag"
        elif not Path(index_dir_cfg).is_absolute():
            index_dir = Path(workspace) / index_dir_cfg
        else:
            index_dir = Path(index_dir_cfg)
        reranker_path = cfg.get("reranker_path") if cfg.get("reranker_enabled") else None
        try:
            return cls(embedder, index_dir, reranker_path=reranker_path, config=cfg)
        except Exception as e:
            print(f"[rag] 实例化失败：{e}")
            return None

    def _init_db(self):
        self.conn.execute("""CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY, file_path TEXT, start_line INT, end_line INT, text TEXT)""")
        self.conn.commit()

    def _new_index(self):
        idx = faiss.IndexHNSWFlat(self.dim, 32, faiss.METRIC_INNER_PRODUCT)
        idx.hnsw.efConstruction = 200
        idx.hnsw.efSearch = 64
        return idx

    # ---------- 切片（按行滑窗，带起止行号）----------
    def _iter_chunks(self, path: Path, lines_per=60, overlap=15):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        i = 0
        while i < len(lines):
            piece = lines[i:i + lines_per]
            text = "\n".join(piece).strip()
            if text:
                yield (str(path), i + 1, i + len(piece), text)
            if i + lines_per >= len(lines):
                break
            i += lines_per - overlap

    # ---------- 建库 ----------
    def index_dir(self, root, exts=(".md", ".txt", ".json", ".py", ".xml"),
                  lines_per=60, overlap=15, batch=32, exclude_globs=None, on_progress=None):
        """扫描 root 下 exts 文件，排除 exclude_globs(fnmatch)，切片+向量化入库。
        on_progress(done, total, last_file) 每处理完一个文件回调（UI 进度用）。
        每次清空重建（千万级的增量/断点续建后续做）。返回 {files, chunks, elapsed}。"""
        root = Path(root)
        with self._lock:   # 与 query 互斥：建库全程持锁（查询会等建库完）
            self.index = self._new_index()
            self.conn.execute("DELETE FROM chunks"); self.conn.commit()
            self._next_id = 0
            ex = exclude_globs or []
            files = [p for p in root.rglob("*")
                     if p.is_file() and p.suffix.lower() in exts
                     and not any(fnmatch.fnmatch(p.name, g) for g in ex)]
            buf = []
            t0 = time.time()
            for i, p in enumerate(files, 1):
                for chunk in self._iter_chunks(p, lines_per, overlap):
                    buf.append(chunk)
                    if len(buf) >= batch:
                        self._flush(buf); buf = []
                if on_progress:
                    try:
                        on_progress(i, len(files), str(p))
                    except Exception:
                        pass
            if buf:
                self._flush(buf)
            faiss.write_index(self.index, str(self.faiss_path))
            self.conn.commit()
            n = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            elapsed = time.time() - t0
            print(f"[rag] {len(files)} 文件 → {n} 片段，建库耗时 {elapsed:.1f}s")
            return {"files": len(files), "chunks": n, "elapsed": elapsed}

    def _flush(self, chunks):
        texts = [c[3] for c in chunks]
        vecs = self.embedder.encode(texts, batch_size=len(texts),
                                    normalize_embeddings=True, show_progress_bar=False)
        vecs = np.ascontiguousarray(vecs, dtype="float32")
        self.index.add(vecs)
        rows = [(self._next_id + i, c[0], c[1], c[2], c[3]) for i, c in enumerate(chunks)]
        self._next_id += len(chunks)
        self.conn.executemany("INSERT INTO chunks VALUES (?,?,?,?,?)", rows)
        self.conn.commit()

    # ---------- 查询 ----------
    def query(self, question, top_k=None, rerank_pool=None):
        """语义召回 top-K 片段（带 file_path/start_line/end_line/text）。
        top_k/rerank_pool 默认从 config 取。"""
        if top_k is None:
            top_k = self.config.get("top_k", 5)
        if rerank_pool is None:
            rerank_pool = self.config.get("rerank_pool", 0)
        with self._lock:   # 与 index_dir 互斥
            qv = self.embedder.encode([question], normalize_embeddings=True, show_progress_bar=False)
            qv = np.ascontiguousarray(qv, dtype="float32")

            k = rerank_pool if (self.reranker and rerank_pool and rerank_pool > top_k) else top_k
            D, I = self.index.search(qv, k)
            ids = [int(i) for i in I[0] if i >= 0]
            if not ids:
                return []
            ph = ",".join("?" * len(ids))
            rows = self.conn.execute(
                f"SELECT id,file_path,start_line,end_line,text FROM chunks WHERE id IN ({ph})", ids).fetchall()
            by_id = {r[0]: r for r in rows}
            results = [by_id[i] for i in ids if i in by_id]  # 保持 ANN 排序

            if self.reranker and rerank_pool and rerank_pool > top_k:
                scores = self.reranker.predict([(question, r[4]) for r in results])
                results = [r for _, r in sorted(zip(scores, results), key=lambda x: -x[0])]

            return [{"file_path": r[1], "start_line": r[2], "end_line": r[3], "text": r[4]}
                    for r in results[:top_k]]

    def stats(self):
        return {"ready": self.index.ntotal > 0, "total_docs": self.index.ntotal, "dim": self.dim}


# ---------- 全局单例（web.py 启动注入，rag_query 工具读取）----------
_rag_instance = None
_rag_workspace = None


def set_rag(instance, workspace=None):
    """web.py 启动/重建时注入当前 LocalRAG 实例。"""
    global _rag_instance, _rag_workspace
    _rag_instance = instance
    _rag_workspace = workspace


def get_rag():
    return _rag_instance


def rag_query(query: str, top_k: int = 5) -> str:
    """在本地文档库做语义搜索（RAG）。返回多行，每行 `相对路径:起行-止行: 片段预览`，共 top_k 条。
    用于回答涉及本地项目文档/设计/代码的问题。未建库或无匹配时返回提示文本。"""
    rag = _rag_instance
    if rag is None or rag.index.ntotal == 0:
        return "(RAG 索引未建立，请先在 /rag 页面配置并建库)"
    try:
        hits = rag.query(query, top_k=top_k)
    except Exception as e:
        return f"[rag_query 出错] {e}"
    if not hits:
        return "(未找到相关文档)"
    base = Path(_rag_workspace) if _rag_workspace else None
    out = []
    for h in hits:
        fp = Path(h["file_path"])
        rel = fp.name
        if base:
            try:
                rel = fp.relative_to(base).as_posix()
            except ValueError:
                pass
        out.append(f"{rel}:{h['start_line']}-{h['end_line']}: "
                   f"{h['text'].strip().replace(chr(10), ' ')[:200]}")
    return "\n".join(out)


def make_rag_tools():
    from tools import Tool
    return [Tool(rag_query)]


if __name__ == "__main__":
    from config import load_rag_config
    ws = Path(__file__).resolve().parent.parent
    cfg = load_rag_config(ws)
    if not cfg.get("embed_model_path"):  # demo 兜底
        cfg.update(embed_model_path=r"D:\models\bge-small-zh-v1.5",
                   docs_dir=r"D:\Projects\BunkerProject\Docs\Liskarm",
                   exts=[".md", ".txt", ".json"], top_k=3)
    rag = LocalRAG.from_config(ws, cfg)
    docs = cfg.get("docs_dir") or "."
    rag.index_dir(docs, exts=tuple(cfg.get("exts", [".md"])),
                  exclude_globs=cfg.get("exclude_globs"),
                  lines_per=cfg.get("lines_per", 60), overlap=cfg.get("overlap", 15),
                  batch=cfg.get("batch", 32))
    for q in ["配置表如何加载到运行时", "核心流程是什么", "数据驱动的对象生成"]:
        print(f"\n=== Q: {q} ===")
        for h in rag.query(q):
            print(f"  {Path(h['file_path']).name}:{h['start_line']}-{h['end_line']}"
                  f"  {h['text'][:80].replace(chr(10), ' ')}")
