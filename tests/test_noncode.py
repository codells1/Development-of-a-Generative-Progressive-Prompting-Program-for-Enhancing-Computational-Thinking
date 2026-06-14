# -*- coding: utf-8 -*-
"""비코드 트랙(패턴인식·분해) 단위 테스트. (quiz_spec §4.B)

실행:  python tests/test_noncode.py
- 패턴인식: 규칙(공차·공비·주기)으로 정답 항 계산·자동 검증 (LM Studio 불필요).
- 분해: LLM 저작은 mock으로 대체해 구조 검증·intent-first 메타만 확인.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402


# ── 패턴인식 (규칙 자동 검증) ──────────────────────────────────────

def test_pattern_term_rules():
    assert api._pattern_term({"type": "arithmetic", "start": 3, "step": 3}, 4) == 15
    assert api._pattern_term({"type": "geometric", "start": 2, "ratio": 3}, 3) == 54
    assert api._pattern_term({"type": "cycle", "seq": ["A", "B", "C"]}, 4) == "B"  # 4 % 3 = 1


def test_build_pattern_verifies_and_4_distinct():
    for _ in range(40):                      # 무작위 규칙 여러 번
        q = api._build_pattern_question()
        assert q is not None
        assert q["type"] == "pattern" and q["track"] == "noncode"
        assert q["verify_method"] == "rule" and q["verified"] is True
        assert q["answer_type"] == "conceptual"
        bodies = [o.split(". ", 1)[-1] for o in q["options"]]
        assert len(set(bodies)) == 4, bodies         # 보기 4개 상이(정답 유일)
        assert q["answer"] in ("A", "B", "C", "D")
        assert api._verify_pattern_question(q) is True   # 규칙 재계산 일치


def test_pattern_answer_is_rule_computed():
    q = api._build_pattern_question()
    rule = q["_pattern_rule"]
    show_n = (max(api._PATTERN_SHOW, len(rule["seq"]) + 1)
              if rule["type"] == "cycle" else api._PATTERN_SHOW)
    expect = str(api._pattern_term(rule, show_n))
    ans_body = next(o.split(". ", 1)[-1] for o in q["options"] if o.startswith(q["answer"] + "."))
    assert ans_body == expect == q["_pattern_answer"]


def test_pattern_verify_rejects_tampered():
    q = api._build_pattern_question()
    wrong = next(L for L in "ABCD" if L != q["answer"])
    assert api._verify_pattern_question(dict(q, answer=wrong)) is False


# ── 분해 (intent-first, LLM mock) ──────────────────────────────────

_GOOD_DECOMP = json.dumps({
    "situation": "민수는 라면을 끓이려고 한다. 냄비에 물을 받아 불에 올리고, 물이 끓으면 면과 스프를 넣고, 다 익으면 그릇에 담는다.",
    "steps": ["물 끓이기", "면과 스프 넣기", "그릇에 담기"],
    "explanation": "준비-조리-마무리의 시간 순서로 나뉜다.",
}, ensure_ascii=False)


def test_decomp_distractors_distinct():
    steps = ["물 끓이기", "면 넣기", "담기"]
    ds = api._decomp_distractors(steps)
    assert len(ds) >= 3
    assert all(d != steps for d in ds)              # 정답 순서와 상이
    assert len({tuple(d) for d in ds}) == len(ds)   # 상호 유일


def test_build_decomposition_answer_is_canonical_steps():
    orig = api.llm.call_decomposition_gen
    api.llm.call_decomposition_gen = lambda templates="": _GOOD_DECOMP
    try:
        q = api._build_decomposition_question()
    finally:
        api.llm.call_decomposition_gen = orig
    assert q is not None
    assert q["type"] == "decomposition" and q["track"] == "noncode"
    assert q["verify_method"] == "authored" and q["verified"] is False  # 사람 검수 대상
    assert len(q["options"]) == 4
    bodies = [o.split(". ", 1)[-1] for o in q["options"]]
    assert len(set(bodies)) == 4                    # 보기 4개 상이
    # 정답 보기 = 모델이 정한 올바른 단계 순서(구조적으로 확정)
    ans_body = next(o.split(". ", 1)[-1] for o in q["options"] if o.startswith(q["answer"] + "."))
    assert ans_body == "물 끓이기 → 면과 스프 넣기 → 그릇에 담기", ans_body
    assert "라면" in q["question"]                  # situation이 stem에 포함


def test_build_decomposition_rejects_bad():
    orig = api.llm.call_decomposition_gen
    api.llm.call_decomposition_gen = lambda templates="": '{"steps": ["하나만"]}'  # 단계 부족
    try:
        q = api._build_decomposition_question()
    finally:
        api.llm.call_decomposition_gen = orig
    assert q is None                                # 구조 검증 실패 → 폴백


# ── 슬롯 교체 (회귀) ───────────────────────────────────────────────

def test_apply_noncode_replaces_pattern_and_decomp():
    orig = api.llm.call_decomposition_gen
    api.llm.call_decomposition_gen = lambda templates="": _GOOD_DECOMP
    qs = [{"ct_skill": s, "question": "code-q", "options": ["A.x", "B.y", "C.z", "D.w"],
           "answer": "A", "answer_type": "conceptual"}
          for s in ("분해", "패턴인식", "추상화", "알고리즘적사고", "통합")]
    try:
        out = api._apply_noncode_questions(qs)
    finally:
        api.llm.call_decomposition_gen = orig
    by = {q["ct_skill"]: q for q in out}
    assert by["분해"].get("type") == "decomposition"
    assert by["패턴인식"].get("type") == "pattern"
    for s in ("추상화", "알고리즘적사고", "통합"):     # 나머지는 그대로(회귀 0)
        assert by[s]["question"] == "code-q" and by[s].get("type") is None


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
