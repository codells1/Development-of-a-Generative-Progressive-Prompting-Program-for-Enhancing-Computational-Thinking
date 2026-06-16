# -*- coding: utf-8 -*-
"""해설 ↔ 정답 일관성 검사 단위 테스트.

실행:  python tests/test_explanation_consistency.py
검증 대상:
  - api._explanation_consistent_with_answer : 해설이 확정 정답과 모순되는지(순수 함수).
  - api._fallback_explanation               : 폴백 해설은 그 자체로 항상 정답과 일관.
LM Studio 불필요 — LLM 비의존 순수 함수만 검사한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api  # noqa: E402

consistent = api._explanation_consistent_with_answer


# ── 숫자 정답: 정답 숫자가 해설에 나오면 일관 ──────────────────────
def test_numeric_answer_present_is_consistent():
    assert consistent("1+2+3을 더하면 6이 됩니다. 정답은 6입니다.", "6")


def test_numeric_answer_with_thousands_comma():
    # 정답 12,000 / 해설은 12000 (천단위 쉼표 표면차) — 같은 수이므로 일관
    assert consistent("할인 후 합계는 12000원이 됩니다.", "12,000")


# ── 숫자 정답: 정답 숫자가 전혀 없고 다른 수만 결론처럼 말하면 모순 ──
def test_numeric_answer_absent_is_inconsistent():
    # 사용자가 본 버그: 정답은 12000인데 해설은 15000을 결론으로 말함
    assert not consistent("모든 항목을 더하면 총합은 15000원입니다.", "12000")


def test_numeric_answer_wrong_only_number():
    assert not consistent("정답은 45입니다.", "60")


# ── 해설에 숫자가 아예 없으면(서술만) 판정 보류 → 일관 처리 ─────────
def test_numeric_answer_no_numbers_in_explanation_passes():
    assert consistent("반복을 끝까지 돌린 뒤의 누적값이 정답입니다.", "60")


# ── 근거 부족(빈 문자열)은 통과 ─────────────────────────────────────
def test_empty_inputs_pass():
    assert consistent("", "60")
    assert consistent("정답은 60입니다.", "")


# ── 여러 줄(출력 추적) 정답: 정상적인 과정 서술은 오탐 없이 통과 ────
def test_multiline_answer_normal_explanation_passes():
    ans = "1\n3\n6"
    assert consistent("반복마다 누적합을 출력하므로 각 줄이 차례로 나옵니다.", ans)


# ── 비숫자 한 줄 정답: '정답은 X'가 다른 값을 단정하면 모순 ──────────
def test_text_answer_wrong_claim_is_inconsistent():
    assert not consistent("정답은 banana입니다.", "apple")


def test_text_answer_right_claim_is_consistent():
    assert consistent("정답은 apple입니다.", "apple")


# ── 폴백 해설은 그 자체로 항상 정답과 일관해야 한다 ─────────────────
def test_fallback_explanation_is_self_consistent():
    for ans in ("60", "12000", "0", "3.14"):
        exp = api._fallback_explanation(ans)
        assert consistent(exp, ans), (ans, exp)
    # 여러 줄 정답은 일반 문구 — 모순 숫자를 넣지 않으므로 통과
    assert consistent(api._fallback_explanation("1\n3\n6"), "1\n3\n6")


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
