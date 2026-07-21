"""longterm_memory.py —— 跨 session 长期记忆（per-repo，~/.agt/repos/<hash>/memories/）。

三类记忆，对应三种【注入机制】（这是本模块的核心设计）：
  - semantic   事实/偏好（"用户是 Unity 背景""rag.json 在 ~/.agt/repos/<hash>/"）
               → 始终注入：每轮以 system block 全文喂给模型（有字数上限，按最近优先截断）。
                 少而稳定、全局有用，像背景知识常驻。
  - procedural 程序/how-to（"发布 PyPI 流程""ModelScope 空壳 200 要重试"）
               → 渐进披露：system 里只列标题清单；模型需要时调 read_procedure(id) 取全文。
                 仿 .agent/skills 模式——标题便宜常驻，正文按需取。
  - episodic   情境/经历（"上次调试 X 时发现…"）
               → 按需召回：每轮用当前 user_message 做关键词召回 top-K 注入。
                 情境化、数量多，只在相关时出现（关键词匹配，零 embedding 依赖）。

写入由主 Agent 自主决定（add_memory 工具）；用户用 /memory 命令查看/管理。
存储：每类一个 JSONL 文件（append 友好 + 全量扫描），内存缓存（load 一次，mutation 后刷新，
避免 messages_for_llm() 每步读盘）。工厂 make_ltm_tools(agent) 仿 plan_tools.py / wiki.py 惯例。
"""
from __future__ import annotations

import json
import random
import re
import time
from threading import Lock
from typing import Optional

from session import repo_memories_dir
from tools import Tool

TYPES = ("semantic", "episodic", "procedural")

# —— 注入上限（控成本）——
SEMANTIC_CAP = 1500      # 始终注入的事实块总字数上限
EPISODIC_TOPK = 3        # 每轮召回的情境记忆条数
EPISODIC_CAP = 1200      # 情境块总字数上限
LIST_PREVIEW = 60        # list/search 时 content 预览字数

# query 轻量分词：按空白与中英文标点切成短语 token（中文按标点分词够用，无需 jieba）
_TOKEN_SPLIT_RE = re.compile(r"[\s,，。、；;！!？?\.()（）\[\]【】\"'“”‘’/\\]+")


class LongTermMemory:
    """绑定工作区的长期记忆库。三类记忆各一个 JSONL；内存缓存 + 线程安全写。"""

    def __init__(self, workspace):
        from pathlib import Path
        self.workspace = Path(workspace)
        self._dir = repo_memories_dir(self.workspace)
        self._lock = Lock()
        self._items: dict[str, list[dict]] = {t: [] for t in TYPES}
        self._load()

    # ========== 路径 ==========
    def _path(self, type_: str):
        return self._dir / f"{type_}.jsonl"

    # ========== 载入 / 持久化 ==========
    def _load(self):
        """启动时把三类 JSONL 全量读进内存（mutation 直接改内存，再落盘）。"""
        for t in TYPES:
            items = []
            p = self._path(t)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except Exception:
                        pass  # 脏行跳过，绝不炸
            self._items[t] = items

    def _append(self, type_: str, record: dict):
        """新增：追加一行（O(1)，不重写全文件）。"""
        with self._lock:
            with open(self._path(type_), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _rewrite(self, type_: str):
        """更新/删除后：全量重写某一类（原子写：先 .tmp 再 replace）。"""
        p = self._path(type_)
        tmp = p.with_suffix(p.suffix + ".tmp")
        body = "\n".join(json.dumps(r, ensure_ascii=False) for r in self._items[type_])
        with self._lock:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(p)

    # ========== 增删改 ==========
    def add(self, type_: str, title: str, content: str,
            tags: Optional[list] = None, origin_session: str = "") -> dict:
        """记一笔。同 type+title 已存在则【更新】而非新增（防重复沉淀）。返回 {action, id}。"""
        if type_ not in TYPES:
            raise ValueError(f"type 只能是 {TYPES}，收到 {type_}")
        title = (title or "").strip()
        if not title:
            raise ValueError("title 不能为空")
        now = int(time.time())
        for r in self._items[type_]:
            if r["title"] == title:
                r["content"] = content
                if tags is not None:
                    r["tags"] = list(tags)
                r["origin_session"] = origin_session or r.get("origin_session", "")
                r["last_used"] = now
                self._rewrite(type_)
                return {"action": "updated", "id": r["id"]}
        rec = {
            "id": f"{type_[:3]}_{int(time.time() * 1000)}_{random.randint(0, 9999)}",
            "type": type_,
            "title": title,
            "content": content,
            "tags": list(tags or []),
            "origin_session": origin_session,
            "created_at": now,
            "last_used": now,
            "use_count": 0,
        }
        self._items[type_].append(rec)
        self._append(type_, rec)
        return {"action": "added", "id": rec["id"]}

    def update(self, id_: str, **fields) -> bool:
        """按 id 更新 title/content/tags（至少一个）。"""
        rec, t = self._find(id_)
        if not rec:
            return False
        for k in ("title", "content", "tags"):
            if k in fields and fields[k] is not None:
                rec[k] = fields[k]
        rec["last_used"] = int(time.time())
        self._rewrite(t)
        return True

    def delete(self, id_: str) -> bool:
        """按 id 删除一条（遍历三类找）。"""
        for t in TYPES:
            before = len(self._items[t])
            self._items[t] = [r for r in self._items[t] if r["id"] != id_]
            if len(self._items[t]) < before:
                self._rewrite(t)
                return True
        return False

    def _find(self, id_: str) -> tuple[Optional[dict], Optional[str]]:
        """返回 (record, type)；找不到返回 (None, None)。"""
        for t in TYPES:
            for r in self._items[t]:
                if r["id"] == id_:
                    return r, t
        return None, None

    def get(self, id_: str) -> Optional[dict]:
        rec, _ = self._find(id_)
        return rec

    # ========== 查 ==========
    def list(self, type_: Optional[str] = None,
             query: Optional[str] = None) -> list[dict]:
        """列出（可按类型 + 关键词过滤）。query 走整句子串匹配。"""
        types = (type_,) if type_ else TYPES
        out = [r for t in types for r in self._items[t]]
        if query:
            q = query.lower()
            out = [r for r in out
                   if q in (r["title"] + "\n" + r["content"] + "\n"
                            + " ".join(r.get("tags", []))).lower()]
        return out

    @staticmethod
    def _tokens(query: str) -> list[str]:
        """把一句话切成关键词 token（len>=2）。中文靠标点分词，英文靠空格。"""
        return [t for t in _TOKEN_SPLIT_RE.split((query or "").lower()) if len(t) >= 2]

    def search(self, query: str, type_: Optional[str] = None,
               limit: int = 10) -> list[dict]:
        """关键词检索（多 token 命中加权：标题命中权重高）。返回按相关性降序的记录列表。"""
        toks = self._tokens(query)
        if not toks:
            toks = [query.strip().lower()] if (query or "").strip() else []
        if not toks:
            return []
        scored = []
        for r in self.list(type_=type_):
            title = r["title"].lower()
            body = (r["content"] + " " + " ".join(r.get("tags", []))).lower()
            score = 0
            for tk in toks:
                if tk in title:
                    score += 3
                if tk in body:
                    score += 1
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: (-x[0], -x[1].get("created_at", 0)))
        return [r for _, r in scored[:limit]]

    # ========== 注入助手（三种机制的核心）==========
    def static_block(self) -> str:
        """静态层注入块 = semantic 全文（最近优先，截断到 SEMANTIC_CAP）+ procedural 标题清单。
        每轮始终注入（semantic 是背景事实；procedural 标题让模型知道有哪些流程经验可调）。
        两类都空则返回空串（不注入）。"""
        parts = []
        sems = sorted(self._items["semantic"],
                      key=lambda r: r.get("created_at", 0), reverse=True)
        buf, total = [], 0
        for r in sems:
            line = f"- {r['title']}：{r['content']}"
            if total + len(line) > SEMANTIC_CAP:
                rest = len(sems) - len(buf)
                if rest > 0:
                    buf.append(f"- …（还有 {rest} 条事实因篇幅省略，可用 search_memory 查询）")
                break
            buf.append(line)
            total += len(line)
        if buf:
            parts.append("【长期记忆·事实（始终生效）】\n" + "\n".join(buf))
        pros = self._items["procedural"]
        if pros:
            titles = "\n".join(f"- {r['id']}  {r['title']}" for r in pros)
            parts.append("【长期记忆·程序经验（仅标题；需要详情时调 read_procedure(id)）】\n" + titles)
        return "\n\n".join(parts)

    def episodic_block(self, query: str, topk: int = EPISODIC_TOPK) -> str:
        """情境层注入块 = 按当前 user_message 召回的 top-K episodic 记忆。
        无命中返回空串（不注入）。每轮由 session 用当前问题作 query 调用。"""
        hits = self.search(query, type_="episodic", limit=topk)
        if not hits:
            return ""
        buf, total = [], 0
        for r in hits:
            line = f"- [{r['id']}] {r['title']}：{r['content']}"
            if total + len(line) > EPISODIC_CAP:
                break
            buf.append(line)
            total += len(line)
        return "【相关长期记忆·情境（按本轮问题召回，如不相关可忽略）】\n" + "\n".join(buf)

    def overview(self) -> str:
        """三类计数 + 每类最近 3 条标题，供 /memory 概览。"""
        lines = [f"🧠 长期记忆（{self._dir}）"]
        for t in TYPES:
            items = self._items[t]
            lines.append(f"  {t}：{len(items)} 条")
            for r in items[-3:][::-1]:
                lines.append(f"      [{r['id']}] {r['title']}")
        return "\n".join(lines)


# ========== Agent 工具（主 Agent 自主沉淀 / 检索 / 管理）==========
def make_ltm_tools(agent) -> list:
    """生成绑定到指定 Agent 的长期记忆工具。
    agent.ltm 由 Agent.__init__ 创建（per-workspace）；这里只做闭包绑定。"""

    def _ltm():
        return agent.ltm

    def add_memory(type: str, title: str, content: str, tags: str = "") -> str:
        """当你判断本轮出现了【值得跨 session 记住】的经验时，把它记一笔到长期记忆库。
        type：semantic=事实/偏好（始终注入，少而稳定）/ episodic=情境经历（按问题召回）/ procedural=流程经验（渐进披露，需要时调 read_procedure）。
        title：一句话标题（≤30字，便于检索与列表展示，务必精炼达意）。
        content：具体内容（事实本身 / 那次经历 / 操作步骤）。
        tags：可选，逗号分隔标签便于检索（如 '踩坑,publish'）。
        值得记的典型场景：踩坑及解法、用户偏好/背景、重要决策与原因、可复用流程。
        同 type+title 会自动【更新】而非重复记录——所以放心重复调用同主题。"""
        if type not in TYPES:
            return f"[错误] type 只能是 {list(TYPES)}，收到 {type}"
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        try:
            origin = getattr(agent.session, "name", "") or ""
            res = _ltm().add(type, title, content, tag_list, origin_session=origin)
            verb = "更新" if res["action"] == "updated" else "记录"
            return f"✅ 已{verb} {type} 记忆 [{res['id']}]「{title}」"
        except Exception as e:
            return f"[记录失败] {type(e).__name__}: {e}"

    def search_memory(query: str, type: str = "") -> str:
        """在长期记忆库里检索（关键词，标题命中优先）。type 留空=搜全部三类。返回 id/类型/标题/内容预览。"""
        t = type.strip() or None
        if t and t not in TYPES:
            return f"[错误] type 只能是 {list(TYPES)} 或留空"
        hits = _ltm().search(query, type_=t, limit=10)
        if not hits:
            return f"未找到与「{query}」相关的长期记忆"
        lines = [f"找到 {len(hits)} 条："]
        for r in hits:
            preview = r["content"][:LIST_PREVIEW] + ("…" if len(r["content"]) > LIST_PREVIEW else "")
            lines.append(f"- [{r['id']}]({r['type']}) {r['title']}：{preview}")
        return "\n".join(lines)

    def read_procedure(id: str) -> str:
        """取出某条记忆的完整内容。procedural 在 system 里只列了标题，需要详情时用这个；
        也支持传 semantic/episodic 的 id（等价取详情）。"""
        rec = _ltm().get(id)
        if not rec:
            return f"[未找到] 没有 id 为 {id} 的记忆"
        tags = ", ".join(rec.get("tags", [])) or "无"
        return (f"[{rec['id']}]({rec['type']}) {rec['title']}\n"
                f"标签: {tags}\n内容:\n{rec['content']}")

    def update_memory(id: str, content: str = "", title: str = "", tags: str = "") -> str:
        """更新某条长期记忆（至少传 content/title/tags 之一）。tags 为逗号分隔，整体替换。"""
        fields = {}
        if title:
            fields["title"] = title
        if content:
            fields["content"] = content
        if tags:
            fields["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
        if not fields:
            return "[错误] 至少传 content / title / tags 之一"
        ok = _ltm().update(id, **fields)
        return f"✅ 已更新 {id}" if ok else f"[未找到] 没有 id 为 {id} 的记忆"

    def delete_memory(id: str) -> str:
        """按 id 删除一条长期记忆（删除前可用 /memory show <id> 确认）。"""
        ok = _ltm().delete(id)
        return f"🗑️ 已删除 {id}" if ok else f"[未找到] 没有 id 为 {id} 的记忆"

    return [Tool(add_memory), Tool(search_memory), Tool(read_procedure),
            Tool(update_memory), Tool(delete_memory)]
