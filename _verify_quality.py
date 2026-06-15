# -*- coding: utf-8 -*-
"""코드 예제·문제·정답에 틀린 내용이 없는지 독립 검증.
api 내부 함수로 실제 세션을 생성한 뒤, 정답을 코드와 별개로 재계산해 대조한다."""
import sys, io, random
sys.stdout.reconfigure(encoding="utf-8")

import api

N_SESSIONS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
CJK = api._CJK_RE

problems = 0
fails = []          # (session, slot, 사유)
warns = []

def has_cjk(s):
    return bool(CJK.search(s or ""))

def check_common(si, q):
    """모든 유형 공통: 보기 4개·서로 다름·비어있지 않음·정답 인덱스 범위·한자 없음."""
    t = q.get("type")
    opts = q.get("options") or []
    if len(opts) != 4:
        fails.append((si, t, f"보기 {len(opts)}개(4개 아님)"))
    if any(not str(o).strip() for o in opts):
        fails.append((si, t, "빈 보기 존재"))
    if len(set(map(str, opts))) != 4:
        fails.append((si, t, "보기 중복(서로 다르지 않음)"))
    ai = q.get("answer_index")
    if not isinstance(ai, int) or not (0 <= ai < len(opts)):
        fails.append((si, t, f"answer_index 범위 오류({ai})"))
    if has_cjk(q.get("stem")):
        fails.append((si, t, "문항(stem)에 한자"))
    if has_cjk(q.get("explanation")):
        fails.append((si, t, "해설에 한자"))
    # 순서도(diagram) 보기는 Mermaid라 한자 검사 제외(한글 라벨은 정상)
    if t not in ("diagram",):
        for j, o in enumerate(opts):
            if has_cjk(str(o)):
                fails.append((si, t, f"보기{j+1}에 한자: {o!r}"))

def check_order(si, q, pseudo):
    """유형1: 블록을 원래 의사코드 순서로 되돌린 라벨열 = 정답이어야 한다(독립 재계산)."""
    blocks = q.get("blocks") or []
    def first_pos(lines):
        for ln in lines:
            if ln in pseudo:
                return pseudo.index(ln)
        return 1e9
    correct = "-".join(b["label"] for b in sorted(blocks, key=lambda b: first_pos(b["lines"])))
    marked = (q.get("options") or [None])[q.get("answer_index", 0)]
    if correct != marked:
        fails.append((si, "order", f"정답 불일치: 재계산={correct} vs 표시정답={marked}"))

def check_diagram(si, q, code):
    """유형2: 코드에서 정답 순서도를 다시 만들어 표시 정답과 일치하는지 대조."""
    g = api._flow_graph(code)
    if g is None:
        warns.append((si, "diagram", "코드 재분석 불가 — 검증 생략"))
        return
    nodes, edges, _ = g
    truth = api._mermaid(nodes, edges)
    marked = (q.get("options") or [None])[q.get("answer_index", 0)]
    if truth != marked:
        fails.append((si, "diagram", "정답 순서도가 코드 구조와 불일치"))

def check_exec(si, q, code):
    """유형3·4·5: 검증 스니펫을 실제 실행한 결과 = 표시 정답 보기. 그리고 정답은 유일해야."""
    snip = q.get("verification_snippet") or ""
    t = q.get("type")
    if not snip.strip():
        fails.append((si, t, "verification_snippet 비어있음"))
        return
    try:
        out = api._run_verification_snippet(snip, code)
    except Exception as e:
        fails.append((si, t, f"스니펫 실행 실패: {e}"))
        return
    opts = q.get("options") or []
    ai = q.get("answer_index", 0)
    marked = opts[ai] if 0 <= ai < len(opts) else None
    if not api._values_match(out, str(marked)):
        fails.append((si, t, f"실행값='{out}' ≠ 표시정답='{marked}'"))
    # 정답 유일성: 실행값과 일치하는 보기가 정답 외에 또 있으면 모호
    also = [j for j, o in enumerate(opts) if j != ai and api._values_match(out, str(o))]
    if also:
        warns.append((si, t, f"정답 외 보기도 실행값과 일치(인덱스 {also}) — 모호"))

for si in range(1, N_SESSIONS + 1):
    print(f"\n===== 세션 {si}/{N_SESSIONS} 생성 중 =====", flush=True)
    topic = random.choice(api.CODE_TOPIC_HINTS)
    ctx = api.rag_store.retrieve("code_examples", topic)
    code = api._gen_validated_code(topic, ctx, api.TARGET_DIFFICULTY)
    if not code:
        fails.append((si, "code", "코드 생성 실패")); continue

    # --- 코드 자체 검증 ---
    ok, why = api._validate_python_code(code)
    if not ok:
        fails.append((si, "code", f"구조 오류: {why}"))
    if has_cjk(code):
        fails.append((si, "code", "코드에 한자"))
    n = api._code_line_count(code)
    if n > api.CODE_MAX_LINES:
        fails.append((si, "code", f"{n}줄 > {api.CODE_MAX_LINES}"))
    stdout = api._run_code_stdout(code)
    if not stdout or len(stdout.split("\n")) < 2:
        fails.append((si, "code", f"실행 출력 부족: {stdout!r}"))
    print(f"  주제={topic} | {n}줄 | 출력 {len((stdout or '').splitlines())}줄", flush=True)

    pseudo = api._gen_pseudocode(code)
    if not pseudo:
        warns.append((si, "pseudo", "의사코드 생성 실패"))

    qs = api._build_question_set(code, pseudo)
    print(f"  문항 {len(qs)}개 생성", flush=True)
    if len(qs) != 5:
        fails.append((si, "set", f"문항 {len(qs)}개(5개 아님)"))

    for q in qs:
        problems += 1
        check_common(si, q)
        t = q.get("type")
        if t == "order":
            check_order(si, q, pseudo)
        elif t == "diagram":
            check_diagram(si, q, code)
        else:
            check_exec(si, q, code)
        mark = "OK"
        sj = [f for f in fails if f[0] == si]
        print(f"    [{t:12s}] {q.get('ct_skill','')}", flush=True)

print("\n" + "=" * 60)
print(f"검증 완료: 세션 {N_SESSIONS}개, 문항 {problems}개")
print(f"  오류(FAIL): {len(fails)}건")
for s, t, why in fails:
    print(f"    - 세션{s} [{t}] {why}")
print(f"  주의(WARN): {len(warns)}건")
for s, t, why in warns:
    print(f"    - 세션{s} [{t}] {why}")
print("=" * 60)
print("결과:", "✅ 틀린 내용 없음" if not fails else "❌ 문제 발견")
