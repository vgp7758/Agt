"""verify_logging.py —— 日志系统（P0）+ LLM 调用 trace（P1）验证。

用临时 workspace，不污染真实 session/log。测完清理 ~/.agt/repos/<tmp_hash>。
A. log 基础设施：name 未就绪缓冲→就绪 flush、切 session 切文件、直写
B. LLM 空响应重试 trace：mock openai client 返回空 choices，验证 warning+error 留痕
"""
import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from log import configure_logging, get_logger, session_log_path  # noqa: E402
from session import Session, _repo_hash, REPOS_DIR  # noqa: E402


def main():
    tmp = Path(tempfile.mkdtemp(prefix="agt_log_test_"))
    tmphash = _repo_hash(tmp)
    print(f"临时 workspace: {tmp} (hash={tmphash})")
    ok = True
    p2 = None
    try:
        # ===== A. log 基础设施 =====
        print("\n=== A. log 基础设施 ===")
        h = configure_logging(level="DEBUG")
        log = get_logger("test")

        s = Session(system="t", workspace=tmp)
        s._log_handler = h
        h.set_session(tmp, "")  # name 空 → 缓冲
        log.info("首轮进行中的日志（应缓冲，此时无文件）")
        sd = REPOS_DIR / tmphash / "sessions"
        assert not (sd.exists() and list(sd.glob("*.log"))), "缓冲期不应有 .log"
        print("  ✅ name 未就绪：缓冲，无文件")

        s.name = "verify_sess"          # 模拟 _ensure_name 设名
        h.set_session(tmp, "verify_sess")  # 触发 flush
        p = session_log_path(tmp, "verify_sess")
        assert p.exists() and "首轮进行中" in p.read_text(encoding="utf-8")
        print("  ✅ name 就绪：缓冲 flush 到 <name>.log")

        log.info("就绪后直写")
        assert "直写" in p.read_text(encoding="utf-8")
        print("  ✅ 就绪后直写")

        h.set_session(tmp, "sess_two")  # 切 session
        log.info("第二个 session 的日志")
        p2 = session_log_path(tmp, "sess_two")
        assert "第二个 session" in p2.read_text(encoding="utf-8")
        assert "第二个 session" not in p.read_text(encoding="utf-8")
        print("  ✅ 切 session 切日志文件（互不污染）")

        # ===== B. LLM 空响应重试 trace（mock openai）=====
        print("\n=== B. LLM 空响应重试 trace（mock openai client）===")
        try:
            from llm_client import LLMClient

            class _Resp:
                def __init__(self, choices): self.choices = choices; self.usage = None
                def model_dump(self): return {"choices": [getattr(c, "message", None) for c in self.choices]}

            class _Msg:
                def model_dump(self): return {"content": "hello back", "reasoning_content": "", "tool_calls": None}

            class _Choice:
                def __init__(self, msg): self.message = msg

            class _Usage:
                def model_dump(self): return {"total_tokens": 42}

            class _RespOk(_Resp):
                def __init__(self): self.choices = [_Choice(_Msg())]; self.usage = _Usage()

            class _Compl:
                def __init__(self, returns): self.returns = returns; self.i = 0
                def create(self, **kw):
                    r = self.returns[self.i]; self.i += 1; return r

            class _Chat:
                def __init__(self, returns): self.completions = _Compl(returns)

            class _Client:
                def __init__(self, returns): self.chat = _Chat(returns)

            client = LLMClient(max_retries=2)
            client._client = _Client([_Resp([]), _Resp([])])  # 两次空 choices
            try:
                client.chat([{"role": "user", "content": "hi"}])
                print("  ⚠️ 预期 RuntimeError 却成功"); ok = False
            except RuntimeError:
                pass
            c = p2.read_text(encoding="utf-8")
            assert "空响应" in c and "连续 2 次空响应" in c, f"trace 缺失:\n{c}"
            print("  ✅ 空响应重试 warning + 放弃 error 全留痕")

            client._client = _Client([_RespOk()])
            client.chat([{"role": "user", "content": "hi"}])
            c2 = p2.read_text(encoding="utf-8")
            assert "成功" in c2 and "tokens=42" in c2, f"成功 trace 缺失:\n{c2}"
            print("  ✅ 成功 trace（model/tokens/耗时）留痕")
        except Exception as e:
            print(f"  ⚠️ B 跳过: {type(e).__name__}: {e}"); ok = False

        print("\n📄 sess_two.log 内容：")
        print(p2.read_text(encoding="utf-8") if p2 and p2.exists() else "(空)")
        print("\n🎉 验证通过" if ok else "\n⚠️ 部分未通过（见上）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        rd = REPOS_DIR / tmphash
        if rd.exists():
            shutil.rmtree(rd, ignore_errors=True)
            print(f"(已清理 ~/.agt/repos/{tmphash})")


if __name__ == "__main__":
    main()
