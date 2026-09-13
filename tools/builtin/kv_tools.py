"""kv_tools.py —— 应用级 KV 结果缓存（外置件，状态随本文件走）。

用途：同输入结果确定的 LLM 调用（如关键词提取）做 memoization——同轮多个
before_turn 工作流共用一次提取。namespace 兼作版本号：改提示词/换模型时
换 namespace 即整体失效。

状态 _KV_CACHE 是进程级 dict（自写自读，纯工具组状态）——随外置件走，
重启清空（结果缓存语义，丢失=下次重新计算，无正确性影响）。
改完本文件用 /reload tools 热加载（/reload 会重建模块，缓存随旧模块丢弃）。

2026-09-13：+kv_cache_claim（原子互斥提取，用户实测竞态修复）——
  旧模式「read(miss) → 工作流节点 write pending → LLM → write 结果」的 read 与
  write pending 分处两个节点非原子：同 hook 双工作流（ThreadPoolExecutor 并发）
  毫秒级同时 read 都 miss → 双双越过互斥同时打本地模型（单并发）→ 一个失败、
  pending 残留（后续轮同消息傻等 90×2s 超时）。claim 把 check-and-set 收进
  进程级锁，并带 pending TTL 接管（提取方死亡后下一 claim 方自动接管）。
"""
from __future__ import annotations

import hashlib
import threading
import time

_KV_CACHE: dict = {}
_KV_LOCK = threading.Lock()
_KV_PENDING_TS: dict = {}          # claim 写 pending 的时刻（TTL 接管判据）
PENDING_TTL = 150.0                # pending 超过此秒数视为提取方死亡——新 claim 方接管
                                   # （< 等待循环上限 90×2s=180s：等待方仍有空表兜底）


def _kv_key(key: str, namespace: str) -> tuple:
    """缓存键：namespace + 内容哈希（超长消息也不占内存，value 原样存）。"""
    h = hashlib.sha1(str(key).encode("utf-8", errors="ignore")).hexdigest()
    return (str(namespace or ""), h)


def kv_cache_read(key: str, namespace: str = "") -> dict:
    """读应用级 KV 缓存：命中返回 {"hit": true, "value": ...}，未命中 {"hit": false, "value": null}。
    key 任意字符串（通常接 user_message 原文，内部按内容哈希存储）；namespace 隔离不同用途/版本。
    进程级存储：重启清空（结果缓存语义，丢失=下次重新计算，无正确性影响）。"""
    v = _KV_CACHE.get(_kv_key(key, namespace))
    return {"hit": v is not None, "value": v}


def kv_cache_write(key: str, value, namespace: str = "") -> dict:
    """写应用级 KV 缓存：把 value（任意 JSON 类型：list/dict/string/number...）存到 key 下，
    与 kv_cache_read 配对（read 未命中 → 计算 → 写回）。返回 {"ok": true}。
    提取方写回真实结果用这里（顺带清 pending 时间戳——TTL 接管判据随结果落定失效）。"""
    k = _kv_key(key, namespace)
    _KV_CACHE[k] = value
    _KV_PENDING_TS.pop(k, None)
    return {"ok": True}


def kv_cache_claim(key: str, namespace: str = "", pending: str = "pending") -> dict:
    """原子互斥提取 claim（2026-09-13·用户实测竞态修复）：三态单次判定——
      - 缓存有真实值            → {"state": "hit",     "value": <真实值>}
      - 有 pending 且未超 TTL    → {"state": "pending", "value": null}（对方正在提取，调用方等待/轮询）
      - 无值 / pending 已超 TTL  → 原子写入 pending 并返回 {"state": "claimed", "value": null}
                                  （**调用方即提取方**——独占计算权，完成后 kv_cache_write 写回）
    check-and-set 在 _KV_LOCK 内完成：并发多线程同时 claim 同一 key，恰好一个 claimed
    （旧 read→write 两节点模式有毫秒级双 miss 窗口——同时打 LLM，本地单并发模型必炸一个）。
    pending TTL（PENDING_TTL）：提取方 LLM 失败/超时永远不写回时，pending 不再永久残留——
    下一个 claim 方超时接管重新提取（等待方 90×2s 空表兜底不受影响）。"""
    with _KV_LOCK:
        k = _kv_key(key, namespace)
        v = _KV_CACHE.get(k)
        if v is None:
            _KV_CACHE[k] = pending
            _KV_PENDING_TS[k] = time.time()
            return {"state": "claimed", "value": None}
        if v == pending:
            if time.time() - _KV_PENDING_TS.get(k, 0) > PENDING_TTL:
                _KV_PENDING_TS[k] = time.time()   # 接管：刷新时刻，本方成为新提取方
                return {"state": "claimed", "value": None}
            return {"state": "pending", "value": None}
        return {"state": "hit", "value": v}


def agt_register():
    return [
        {"name": "kv_cache_read", "func": kv_cache_read, "hidden": True, "group": "light",
         "version": 1, "outputs": [
             {"name": "hit", "type": "boolean", "description": "是否命中缓存"},
             {"name": "value", "type": "any", "description": "缓存的值（未命中为 null）"},
         ], "params": {
             "key": "缓存键（通常接 user_message 原文，内部按内容哈希）",
             "namespace": "命名空间：隔离不同用途，兼作版本号（改提示词/换模型时换名即整体失效）",
         }},
        {"name": "kv_cache_write", "func": kv_cache_write, "hidden": True, "group": "light",
         "version": 2, "params": {
             "key": "缓存键（与配套 read 相同的 key）",
             "value": "要缓存的值（任意 JSON 类型）",
             "namespace": "命名空间（与配套 read 相同）",
         }},
        {"name": "kv_cache_claim", "func": kv_cache_claim, "hidden": True, "group": "light",
         "version": 1, "outputs": [
             {"name": "state", "type": "string", "description": "claimed=调用方即提取方（已原子占位）/ pending=对方提取中（等待轮询）/ hit=命中真实值"},
             {"name": "value", "type": "any", "description": "命中时的真实值（否则 null）"},
         ], "params": {
             "key": "缓存键（与配套 read/write 相同的 key）",
             "namespace": "命名空间（与配套 read/write 相同）",
             "pending": "占位标记值（默认 'pending'；等待方轮询 read 到值≠此标记即为结果就绪）",
         }},
    ]
