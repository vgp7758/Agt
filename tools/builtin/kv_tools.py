"""kv_tools.py —— 应用级 KV 结果缓存（外置件，状态随本文件走）。

用途：同输入结果确定的 LLM 调用（如关键词提取）做 memoization——同轮多个
before_turn 工作流共用一次提取。namespace 兼作版本号：改提示词/换模型时
换 namespace 即整体失效。

状态 _KV_CACHE 是进程级 dict（自写自读，纯工具组状态）——随外置件走，
重启清空（结果缓存语义，丢失=下次重新计算，无正确性影响）。
改完本文件用 /reload tools 热加载（/reload 会重建模块，缓存随旧模块丢弃）。
"""
from __future__ import annotations

import hashlib

_KV_CACHE: dict = {}


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
    与 kv_cache_read 配对（read 未命中 → 计算 → 写回）。返回 {"ok": true}。"""
    _KV_CACHE[_kv_key(key, namespace)] = value
    return {"ok": True}


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
         "version": 1, "params": {
             "key": "缓存键（与配套 read 相同的 key）",
             "value": "要缓存的值（任意 JSON 类型：list/dict/string/number...）",
             "namespace": "命名空间（与配套 read 相同）",
         }},
    ]
