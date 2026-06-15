# -*- coding: utf-8 -*-
"""RAG 검색 type 필터 단위 테스트 (question_generation_revision §8).

실행:  python tests/test_retrieval_filter.py
검증:
  - problem_templates RAG는 'template' 파일이 지정된 슬롯(유형 3·4·5)에서만 호출되고,
    그 슬롯의 source_file로 filter가 걸린다(섞임 방지).
  - 유형 1(order, template 없음)은 RAG를 호출하지 않고 "" 를 돌려준다.
LM Studio 불필요 — rag_store.retrieve를 가로채 호출 인자만 확인한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402


def test_retrieve_template_for_uses_slot_filter():
    calls = []

    def fake_retrieve(collection, query, k=5, filter=None):
        calls.append({"collection": collection, "query": query, "k": k, "filter": filter})
        return f"[{filter['source_file']}] 가이드"

    orig = api.rag_store.retrieve
    api.rag_store.retrieve = fake_retrieve
    try:
        with_tpl = [s for s in api.PROBLEM_SLOTS if s.get("template")]
        assert with_tpl, "template 지정 슬롯이 있어야 한다"
        for slot in with_tpl:
            calls.clear()
            out = api._retrieve_template_for(slot, "print('hello')")
            assert len(calls) == 1, calls
            c = calls[0]
            assert c["collection"] == "problem_templates"
            assert c["filter"] == {"source_file": slot["template"]}, (slot, c["filter"])
            assert slot["ct_skill"] in c["query"]
            assert out  # 가이드 문자열 반환
    finally:
        api.rag_store.retrieve = orig


def test_retrieve_template_for_skips_when_no_template():
    calls = []

    def fake_retrieve(collection, query, k=5, filter=None):
        calls.append(1)
        return "X"

    orig = api.rag_store.retrieve
    api.rag_store.retrieve = fake_retrieve
    try:
        no_tpl = [s for s in api.PROBLEM_SLOTS if not s.get("template")]
        assert no_tpl, "template 없는 슬롯(유형1 등)이 있어야 한다"
        for slot in no_tpl:
            assert api._retrieve_template_for(slot, "print('x')") == ""
        assert calls == [], "template 없는 슬롯은 RAG를 호출하면 안 된다"
    finally:
        api.rag_store.retrieve = orig


def test_retrieve_signature_has_filter():
    # rag.retrieve 가 filter 파라미터를 받는다(엔진 패스스루)
    import inspect
    import rag
    params = inspect.signature(rag.retrieve).parameters
    assert "filter" in params, list(params)


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}  →  {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}  →  {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
