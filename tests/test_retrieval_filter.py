# -*- coding: utf-8 -*-
"""RAG 검색 type 필터(quiz_spec §7) 단위 테스트.

실행:  python tests/test_retrieval_filter.py
검증: 각 CT 유형이 자기 코퍼스(ct_{유형}.txt)에만 filter를 걸어 retrieve하는지(섞임 방지).
LM Studio 불필요 — rag_store.retrieve를 가로채 호출 인자만 확인한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402


def test_retrieve_templates_filters_per_skill():
    calls = []

    def fake_retrieve(collection, query, k=5, filter=None):
        calls.append({"collection": collection, "query": query, "k": k, "filter": filter})
        return f"[{filter['source_file']}] 가이드"

    orig = api.rag_store.retrieve
    api.rag_store.retrieve = fake_retrieve
    try:
        api._retrieve_templates("print('hello')")
    finally:
        api.rag_store.retrieve = orig

    # 5유형 각각 자기 파일로 필터링
    assert len(calls) == len(api.PROBLEM_CT_SKILLS), calls
    for c, skill in zip(calls, api.PROBLEM_CT_SKILLS):
        assert c["collection"] == "problem_templates"
        assert c["filter"] == {"source_file": f"ct_{skill}.txt"}, (skill, c["filter"])
        assert skill in c["query"]


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
