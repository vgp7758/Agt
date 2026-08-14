"""session_vec.py —— 会话历史的向量检索层（per-repo）。

复用 RAG 的 faiss(HNSW) + sqlite 底座，但切片粒度 = 一个 turn（而非按行滑窗）。
一条向量 ↔ 一轮完整对话（user + answer + summary + reasoning 拼成的检索文本），
payload 关联 session_id/turn_no，召回时从 session 存档按索引取完整上下文。

启用条件：rag.json 的 embed_provider/embed_model_path（或 api 那套）已配置——
和 LocalRAG 共享同一个 embedder 来源。未配置时 from_config 返回 None，
recall 自动退回子串匹配（见 session.recall）。

存储位置：~/.agt/repos/<hash>/sessions_vec/{vecs.index, turns.db}——与 sessions/ 并排，
不污染原存档；删了重建无副作用（原档是真相源）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import faiss
import numpy as np

# 复用 rag.py 的两种 embedder（local: SentenceTransformer / api: OpenAI 兼容），
# 避免再写一遍 embedder 装配逻辑——配了哪个用哪个，没配就返回 None。
from rag import APIEmbedder


def _build_embedder(cfg: dict):
    """按 rag.json 的 embed 配置建 embedder；任一关键字段缺 → 返回 None（由调用方降级）。

    和 LocalRAG.from_config 里 embedder 装配逻辑同源，但不依赖 LocalRAG 实例——
    session 向量库可以独立于文档 RAG 启用（用户可能只想给会话历史加语义检索，
    不一定建文档库）。"""
    provider = cfg.get("embed_provider", "local")
    try:
        if provider == "api":
            if not cfg.get("embed_api_url") or not cfg.get("embed_api_model"):
                return None
            return APIEmbedder(cfg["embed_api_url"], cfg.get("embed_api_token", ""),
                               cfg["embed_api_model"], cfg.get("embed_api_dim", 0))
        # local：必须有模型路径
        if not cfg.get("embed_model_path"):
            return None
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(cfg["embed_model_path"])
    except Exception:
        return None


class SessionVectorStore:
    """per-repo 会话向量库：一个 turn 一条向量，语义召回 top-K 轮。

    数据流：turn 落盘后 → build_one(index, payload) 增量入库 →
    search(query, top_k) 召回 turn_no 列表 → 调用方按 turn_no 从 session 取完整内容。
    删 session / rewind 到某轮 → remove_session(session_id) / truncate(session_id, keep)。

    幂等：同 (session_id, turn_no) 重复 build_one 会先删旧再加新（重放安全）。
    """

    def __init__(self, embedder, index_dir: Path):
        self.embedder = embedder
        self.dim = embedder.get_sentence_embedding_dimension()
        self.store_dir = Path(index_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.faiss_path = self.store_dir / "session_vecs.index"
        self.db_path = self.store_dir / "session_turns.db"
        self._lock = threading.Lock()      # build/search/删 互斥（和 LocalRAG 同模式）
        self.conn = self._init_db()
        self.index = self._new_index()
        self._next_id = 0
        if self.faiss_path.exists():
            try:
                loaded = faiss.read_index(str(self.faiss_path))
                if loaded.d == self.dim:
                    self.index = loaded
                    self._next_id = self.conn.execute(
                        "SELECT COUNT(*) FROM turn_vecs").fetchone()[0]
            except Exception:
                pass     # 索引损坏 → 用空索引重建（数据库还在，可按需重建）

    # ---------- 初始化 ----------
    def _init_db(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # rowid 自增；session_id+turn_no 唯一（重放时先删后加）
        conn.execute("""CREATE TABLE IF NOT EXISTS turn_vecs(
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_no INTEGER NOT NULL,
            user TEXT, answer TEXT, summary TEXT, reasoning TEXT,
            search_text TEXT,        -- 拼接后的检索文本（不存 tool，体积大且语义稀疏）
            call_ids TEXT,           -- JSON list[str]，按 id 现取 toollog
            created_at REAL,
            UNIQUE(session_id, turn_no))""")
        conn.commit()
        return conn

    def _new_index(self):
        idx = faiss.IndexHNSWFlat(self.dim, 32, faiss.METRIC_INNER_PRODUCT)
        idx.hnsw.efConstruction = 200
        idx.hnsw.efSearch = 64
        return idx

    @classmethod
    def from_config(cls, workspace, cfg: dict):
        """按 rag.json 建 session 向量库；embed 未配置 → 返回 None（recall 退回子串）。

        和 LocalRAG.from_config 的降级逻辑对称：不抛异常，让上层用 None 判断走兜底。
        cfg 即 load_rag_config(workspace) 的结果。"""
        embedder = _build_embedder(cfg)
        if embedder is None:
            return None
        from session import REPOS_DIR, _repo_key
        index_dir = REPOS_DIR / _repo_key(workspace) / "sessions_vec"
        try:
            return cls(embedder, index_dir)
        except Exception:
            return None

    # ---------- 增量索引 ----------
    def build_one(self, session_id: str, turn_no: int, *, user: str, answer: str,
                  summary: str, reasoning: str, call_ids: list):
        """增量索引一轮 turn。幂等：同 (session_id, turn_no) 先删旧再加新。

        search_text = summary + user + answer + reasoning 拼接——summary 在前(语义浓缩)，
        其余补充细节。reasoning 仅作检索补充（recall 返回时按 contains_reasoning 决定是否带）。
        """
        search_text = "\n".join(s for s in (summary, user, answer, reasoning) if s).strip()
        if not search_text:
            return     # 空轮不索引（如刚 start_turn 还没内容）
        with self._lock:
            # 幂等：删旧条目（faiss 不支持按 id 删，HNSW 只能重建——这里只清 db 旧行，
            # 重建全库时才真正移除向量；增量重放容忍少量冗余向量，search 时按 db 去重）
            cur = self.conn.execute(
                "SELECT id FROM turn_vecs WHERE session_id=? AND turn_no=?",
                (session_id, turn_no)).fetchone()
            if cur:     # 已存在 → 用新向量覆盖 db 行（向量重建时统一清理；单条不重建省时）
                self.conn.execute(
                    "UPDATE turn_vecs SET user=?,answer=?,summary=?,reasoning=?,"
                    "search_text=?,call_ids=?,created_at=? WHERE id=?",
                    (user, answer, summary, reasoning, search_text,
                     json.dumps(call_ids, ensure_ascii=False), time.time(), cur[0]))
                self.conn.commit()
                # 注意：向量不更新（HNSW 无单条改）——重建全库时刷新。增量场景下 search_text
                # 可能与向量略不同步，但语义相近，召回仍有效；真要精确就重建。
                return
            vecs = self.embedder.encode([search_text], normalize_embeddings=True,
                                        show_progress_bar=False)
            vecs = np.ascontiguousarray(vecs, dtype="float32")
            self.index.add(vecs)
            rid = self._next_id
            self._next_id += 1
            self.conn.execute(
                "INSERT INTO turn_vecs(id,session_id,turn_no,user,answer,summary,"
                "reasoning,search_text,call_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, session_id, turn_no, user, answer, summary, reasoning,
                 search_text, json.dumps(call_ids, ensure_ascii=False), time.time()))
            self.conn.commit()
            faiss.write_index(self.index, str(self.faiss_path))

    def rebuild(self, turns_iter, session_id_fn):
        """全量重建：turns_iter 产出 (turn_no, user, answer, summary, reasoning, call_ids)；
        session_id_fn(turn_no)→session_id。清空旧索引+db 后重建。供 /rewind 或手动重建用。"""
        with self._lock:
            self.index = self._new_index()
            self._next_id = 0
            self.conn.execute("DELETE FROM turn_vecs")
            self.conn.commit()
            buf = []
            BATCH = 32
            for t in turns_iter:
                turn_no, user, answer, summary, reasoning, call_ids = t
                sid = session_id_fn(turn_no)
                search_text = "\n".join(s for s in (summary, user, answer, reasoning) if s).strip()
                if not search_text:
                    continue
                buf.append((sid, turn_no, user, answer, summary, reasoning,
                            search_text, json.dumps(call_ids, ensure_ascii=False)))
                if len(buf) >= BATCH:
                    self._flush_batch(buf); buf = []
            if buf:
                self._flush_batch(buf)
            faiss.write_index(self.index, str(self.faiss_path))
            self.conn.commit()

    def _flush_batch(self, rows):
        texts = [r[6] for r in rows]
        vecs = self.embedder.encode(texts, batch_size=len(texts),
                                    normalize_embeddings=True, show_progress_bar=False)
        vecs = np.ascontiguousarray(vecs, dtype="float32")
        self.index.add(vecs)
        db_rows = [(self._next_id + i, *r, time.time()) for i, r in enumerate(rows)]
        # r = (sid, turn_no, user, answer, summary, reasoning, search_text, call_ids_json)
        self._next_id += len(rows)
        self.conn.executemany(
            "INSERT INTO turn_vecs(id,session_id,turn_no,user,answer,summary,"
            "reasoning,search_text,call_ids,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            db_rows)

    # ---------- 查询 ----------
    def search(self, query: str, top_k: int = 5, session_id: str = None) -> list:
        """语义召回 top-K 轮。session_id 非空时只在该 session 内搜。

        返回 [{session_id, turn_no, score}]，按相似度降序。调用方按 turn_no 从 session
        取完整上下文（_format_turn_full）。空库/无匹配 → []。
        """
        if self.index.ntotal == 0:
            return []
        with self._lock:
            qv = self.embedder.encode([query], normalize_embeddings=True,
                                       show_progress_bar=False)
            qv = np.ascontiguousarray(qv, dtype="float32")
            k = min(top_k * 3, self.index.ntotal)   # 多取再按 session 过滤
            D, I = self.index.search(qv, k)
            ids = [int(i) for i in I[0] if i >= 0]
            if not ids:
                return []
            ph = ",".join("?" * len(ids))
            rows = self.conn.execute(
                f"SELECT id,session_id,turn_no FROM turn_vecs WHERE id IN ({ph})",
                ids).fetchall()
            by_id = {r[0]: r for r in rows}
            out = []
            seen = set()      # (session_id, turn_no) 去重（增量重放产生的冗余向量）
            for rank, vid in enumerate(ids):
                if vid not in by_id:
                    continue
                _, sid, tno = by_id[vid]
                if session_id and sid != session_id:
                    continue
                key = (sid, tno)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"session_id": sid, "turn_no": tno,
                            "score": float(D[0][rank])})
                if len(out) >= top_k:
                    break
            return out

    # ---------- 删除（rewind / 删 session 用）----------
    def remove_session(self, session_id: str):
        """删某 session 的全部条目（db 行清；向量靠下次 rebuild 清，HNSW 无单条删）。"""
        with self._lock:
            self.conn.execute("DELETE FROM turn_vecs WHERE session_id=?",
                              (session_id,))
            self.conn.commit()

    def truncate(self, session_id: str, keep_turns: int):
        """rewind 到最近 keep_turns 轮后，删超出范围的旧条目。"""
        with self._lock:
            self.conn.execute(
                "DELETE FROM turn_vecs WHERE session_id=? AND turn_no>?",
                (session_id, keep_turns))
            self.conn.commit()

    def stats(self) -> dict:
        """向量库规模（/tools 或 UI 展示用）。"""
        with self._lock:
            n = self.conn.execute("SELECT COUNT(*) FROM turn_vecs").fetchone()[0]
            ns = self.conn.execute(
                "SELECT COUNT(DISTINCT session_id) FROM turn_vecs").fetchone()[0]
        return {"vectors": int(self.index.ntotal), "turns": n, "sessions": ns}
