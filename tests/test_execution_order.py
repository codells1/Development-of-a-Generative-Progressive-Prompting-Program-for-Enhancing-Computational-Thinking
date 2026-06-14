# -*- coding: utf-8 -*-
"""실행 순서 고르기(execution_order) 단위 테스트.

실행:  python tests/test_execution_order.py   (pytest 불필요)
       또는  python -m pytest tests/test_execution_order.py
설계: 실제 생성 코드는 트레이스가 길어 MCQ에 부적합 → '처음 실행되는 순서'(앞 N줄 순열)로 출제.
범위: api.py의 트레이서·라벨링·순서산출·오답·검증 게이트·슬롯 교체(회귀).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402

# 줄(1-index): 1 total=0 / 2 for / 3 total+=s / 4 print
LOOP_CODE = "total = 0\nfor s in [1, 2, 3]:\n    total = total + s\nprint(total)\n"
# 분기: 1 x=5 / 2 if / 3 y='big' / 4 else / 5 y='small' / 6 print
BRANCH_CODE = "x = 5\nif x > 3:\n    y = 'big'\nelse:\n    y = 'small'\nprint(y)\n"
# 함수: 1 def / 2 y=x+1 / 3 return / 4 a=f(5) / 5 print  → 호출이 정의 본문보다 먼저
FUNC_CODE = "def f(x):\n    y = x + 1\n    return y\na = f(5)\nprint(a)\n"


def test_tracer_loop_sequence():
    seq = api._run_tracer(LOOP_CODE)
    # for 줄(2)은 반복마다 + 종료 1회, 본문(3)은 3회
    assert seq == [1, 2, 3, 2, 3, 2, 3, 2, 4], seq


def test_tracer_branch_skips_else():
    seq = api._run_tracer(BRANCH_CODE)
    assert 5 not in seq, seq                       # else 본문(5)은 미실행
    assert seq[0] == 1 and seq[-1] == 6, seq


def test_first_exec_order_func():
    seq = api._run_tracer(FUNC_CODE)               # [1, 4, 2, 3, 5]
    labels = api._labelable_linenos(FUNC_CODE)     # {2, 3, 4, 5} (def 줄 1 제외)
    order = api._first_exec_order(seq, labels)
    # 호출(4)이 함수 본문(2,3)보다 먼저 실행된다.
    assert order == [4, 2, 3, 5], order


def test_labelable_excludes_data_literal():
    # 다중행 리스트 리터럴의 항목 줄(3,4)은 statement가 아니라 라벨에서 빠진다.
    code = "x = 1\nitems = [\n    10,\n    20,\n]\nprint(items)\n"
    labels = api._labelable_linenos(code)
    assert 1 in labels and 2 in labels and 6 in labels   # 대입·대입·print
    assert 3 not in labels and 4 not in labels           # 데이터 항목 줄 제외


def test_labelable_excludes_def_and_import():
    code = "import os\ndef f():\n    return 1\nf()\n"
    labels = api._labelable_linenos(code)
    assert 1 not in labels and 2 not in labels           # import·def 제외
    assert 3 in labels and 4 in labels                   # return·호출


def test_perm_distractors_distinct():
    answer = (2, 0, 1, 3)
    ds = api._perm_distractors(answer)
    assert len(ds) >= 3, ds
    assert all(d != answer for d in ds)            # 정답과 상이
    assert len(set(ds)) == len(ds)                 # 상호 유일
    assert all(sorted(d) == [0, 1, 2, 3] for d in ds)  # 모두 순열


def test_build_and_validate_func():
    q = api._build_execution_order(FUNC_CODE)
    assert q is not None
    assert q["type"] == "execution_order" and q["track"] == "code"
    assert q["verify_method"] == "trace" and q["verified"] is True
    assert q["answer_type"] == "conceptual"        # _verify_one 우회
    assert len(q["options"]) == 4
    bodies = [o.split(". ", 1)[-1] for o in q["options"]]
    assert len(set(bodies)) == 4                   # 보기 4개 모두 상이(정답 유일)
    assert q["answer"] in ("A", "B", "C", "D")
    assert api._validate_execution_order(FUNC_CODE, q) is True


def test_answer_matches_exec_order():
    q = api._build_execution_order(FUNC_CODE)
    ans_body = next(o.split(". ", 1)[-1] for o in q["options"] if o.startswith(q["answer"] + "."))
    # 라벨(소스순): ①=2,②=3,③=4,④=5. 첫 실행 순서 [4,2,3,5] = ③①②④
    assert ans_body == "③ → ① → ② → ④", ans_body


def test_validate_rejects_wrong_answer():
    q = api._build_execution_order(FUNC_CODE)
    wrong = next(L for L in "ABCD" if L != q["answer"])
    assert api._validate_execution_order(FUNC_CODE, dict(q, answer=wrong)) is False


def test_straightline_falls_back():
    # 줄번호 순서 == 실행 순서(함수 없음) → 너무 쉬워서 None(코드형 폴백)
    assert api._build_execution_order(LOOP_CODE) is None


def test_apply_replaces_only_algorithm_slot():
    algo = {"ct_skill": "알고리즘적사고", "question": "old", "options": ["A.x"], "answer": "A",
            "answer_type": "computational"}
    others = [{"ct_skill": s, "question": "q", "options": ["A.x"], "answer": "A",
               "answer_type": "conceptual"} for s in ("분해", "추상화", "통합")]
    out = api._apply_execution_order([algo] + others, FUNC_CODE)
    algo_out = next(q for q in out if q["ct_skill"] == "알고리즘적사고")
    assert algo_out.get("type") == "execution_order"   # 알고리즘 슬롯만 교체
    for s in ("분해", "추상화", "통합"):                # 나머지는 그대로(회귀 없음)
        q = next(q for q in out if q["ct_skill"] == s)
        assert q["question"] == "q" and q.get("type") is None


def test_apply_fallback_on_untraceable():
    algo = {"ct_skill": "알고리즘적사고", "question": "keep", "options": ["A.x", "B.y", "C.z", "D.w"],
            "answer": "A", "answer_type": "conceptual"}
    out = api._apply_execution_order([algo], "x = 1\n")   # 라벨 1개뿐 → 부적합 → 폴백
    assert out[0]["question"] == "keep" and out[0].get("type") is None


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
