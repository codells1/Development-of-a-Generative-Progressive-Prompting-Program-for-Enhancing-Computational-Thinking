from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import random
import re
import os
import ast
import subprocess
import sys
import tempfile
from datetime import datetime
import rag as rag_store
import llm

VERIFY_TIMEOUT_SEC    = 5      # verification_snippet 실행 타임아웃
CODE_MAX_LINES        = 20     # 생성 코드 최대 줄 수(빈 줄 제외). 초과 시 재생성
CODE_GEN_RETRY        = 3      # 코드 길이 초과 시 재생성 최대 횟수
PROBLEM_RETRY_LIMIT   = 3      # 세트 파싱·구조 검증 실패 시 전체 재생성 최대 횟수
VERIFY_RETRY_LIMIT    = 3      # spec §3.4 — 검증 실패한 그 문항만 재생성 최대 횟수
CHAT_LOG_CHAR_BUDGET  = 3500   # CT 평가 시 대화 로그 글자수 상한 (모델 컨텍스트 초과 방지)
MAX_CHAT_HISTORY      = 16     # 챗봇 호출 시 모델에 넣는 직전 대화 최대 메시지 수 (컨텍스트 초과 방지)

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

# 코드 생성용 주제 힌트 (stage 키워드 아님, 개념 키워드). RAG 검색 쿼리 + LLM 주제 힌트로 사용.
CODE_TOPIC_HINTS = (
    "리스트 합계와 평균",
    "딕셔너리 빈도 집계",
    "조건별 분류 처리",
    "할인·요금 계산",
    "단어·문자열 분석",
    "성적·점수 채점",
    "재고·수량 관리",
    "예산·가계부 요약",
)

# 코드·문제 난이도를 한 수준으로 통일한다 (클라이언트 입력과 무관하게 서버에서 고정).
TARGET_DIFFICULTY = "중학교 3학년~고등학교 1학년"

# CT 평가 4요소 (prompts/ct_evaluation_rubric.md). (id, 화면 표시명, SRL여부).
# 모델이 elements 배열을 직접 출력하므로 flat JSON 키는 더 쓰지 않는다.
# CT 총점(내부) = 문제분해+추상화+실행흐름(srl=False). 자기해결은 SRL 지표로 별도 기록(총점 제외).
# 관련 발화가 없는 요소는 모델이 elements에서 생략 → 가변 길이 배열. "NA" 미사용.
CT_ELEMENTS = (
    ("decomposition",   "문제 분해", False),
    ("abstraction",     "추상화",   False),
    ("control_flow",    "실행 흐름", False),
    ("self_resolution", "자기 해결", True),   # SRL — CT 총점 제외
)
_CT_ORDER     = [eid for eid, _name, _srl in CT_ELEMENTS]          # 화면 표시 순서
_ELID_TO_NAME = {eid: name for eid, name, _srl in CT_ELEMENTS}     # id → 표시명
_SRL_IDS      = {eid for eid, _name, srl in CT_ELEMENTS if srl}    # SRL 요소 id

# 문항 슬롯 (question_generation_revision.md §2). 5문항 전부 4지선다.
# code_kind·answer_type은 서버가 슬롯별로 주입한다(모델 신뢰 안 함).
PROBLEM_SLOTS = (
    {"type": "order",        "ct_skill": "문제분해",        "code_kind": "pseudocode", "answer_type": "order"},
    {"type": "diagram",      "ct_skill": "추상화/알고리즘", "code_kind": "pseudocode", "answer_type": "diagram"},
    {"type": "pattern",      "ct_skill": "패턴인식",        "code_kind": "python", "answer_type": "computational", "template": "ct_패턴인식.txt"},
    {"type": "mid_output",   "ct_skill": "코드(중간 출력)", "code_kind": "python", "answer_type": "computational"},
    {"type": "final_output", "ct_skill": "코드(최종 출력)", "code_kind": "python", "answer_type": "computational"},
)

FEEDBACK_FILE = os.path.join(os.path.dirname(__file__), "feedback.json")

# 생성된 문항 세트(focus_points 등 내부 필드 포함)를 session_id로 서버에만 보관한다.
# 챗봇이 유도 방향을 잡을 때 여기서 ct_skill·focus_points를 읽는다. 학생에겐 절대 안 나간다.
PROBLEM_SETS_FILE = os.path.join(os.path.dirname(__file__), "problem_sets.json")


def strip_fences(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def _slice_first_json(raw: str) -> str:
    """
    응답에서 첫 균형 잡힌 JSON 객체 문자열을 brace counting으로 잘라낸다.
    문자열 내부의 { } 와 이스케이프 처리. LLM이 객체를 닫지 않고 다음 객체로 넘어가도
    첫 객체의 끝(균형 시점)에서 끊는다. 균형이 끝까지 안 맞으면 ValueError.
    """
    start = raw.find('{')
    if start == -1:
        raise ValueError("JSON 블록 없음")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape:
            escape = False
            continue
        if in_string:
            if ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return raw[start:i+1]
    raise ValueError(f"균형 잡힌 JSON 객체 없음 (depth={depth} at end)")


def _extract_json(raw: str) -> dict:
    """응답에서 첫 균형 잡힌 JSON 객체를 추출·파싱한다."""
    # strict=False — LLM이 문자열 내부에 raw 개행 등을 그대로 출력해도 허용
    return json.loads(_slice_first_json(raw), strict=False)


NARRATIVE_CHAR_LIMIT = 240   # 서술 피드백 상한(공백 포함). 사양 권장 150~220 + 약간의 여유.


def _clamp_narrative(text: str) -> str:
    """서술 피드백을 박스 규격에 맞춰 상한 강제. 넘치면 마지막 문장 경계에서 자른다."""
    t = (text or "").strip()
    if len(t) <= NARRATIVE_CHAR_LIMIT:
        return t
    cut = t[:NARRATIVE_CHAR_LIMIT]
    end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end >= 80:                       # 의미 있는 분량이 남으면 문장 끝에서 자른다
        return cut[:end + 1].strip()
    return cut.rstrip() + "…"


_VALID_GRADES = {"상", "중", "하"}
_HIGHLIGHT_LIMIT = 6


def _norm_highlights(raw_list) -> list:
    """루브릭의 '근거' 배열을 화면용으로 정리한다.
    각 항목 = {quote(학생 발화 인용), element(요소 표시명), grade(상/중/하), reason(한 줄)}.
    요소(요소/element)는 id(decomposition 등)로 오므로 표시명으로 매핑.
    발화가 비었거나 등급이 상/중/하가 아니면 버린다. 최대 _HIGHLIGHT_LIMIT개."""
    out = []
    if not isinstance(raw_list, list):
        return out
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        quote  = str(item.get("발화")  or item.get("quote")   or "").strip()
        grade  = str(item.get("등급")  or item.get("grade")   or "").strip()
        reason = str(item.get("이유")  or item.get("reason")  or "").strip()
        el     = str(item.get("요소")  or item.get("element") or "").strip()
        # 모델이 라벨을 붙여 인용한 경우 제거
        for tag in ("[학생·채점대상]", "[학생]"):
            if quote.startswith(tag):
                quote = quote[len(tag):].strip()
        if not quote or grade not in _VALID_GRADES:
            continue
        out.append({
            "quote":   quote,
            "element": _ELID_TO_NAME.get(el, el),   # id면 표시명으로, 이미 표시명이면 그대로
            "grade":   grade,
            "reason":  reason,
        })
        if len(out) >= _HIGHLIGHT_LIMIT:
            break
    return out


def score_ct(ct_raw: dict) -> dict:
    """
    루브릭 원시 채점 결과(ct_raw)를 4요소 상/중/하(측정된 것만) + 서술 피드백 + 평가 근거로 정리한다.
    - 새 형식: 모델이 elements 배열을 직접 출력(id·name·grade·srl). 발화 없는 요소는 생략(NA 없음).
    - 여기서는 알려진 id·유효 등급만 통과시키고, 표시명·SRL 플래그를 코드가 채워 순서를 고정한다.
    - 숫자 총점은 만들지 않는다(학생 화면 비노출 사양).
    - highlights: 학생 발화 인용 + 요소·등급·이유 (대화의 어느 부분이 어떻게 평가됐는지).
    LLM 비의존 — 순수 함수라 단독 검증 가능.
    """
    raw_elems = ct_raw.get("elements")
    by_id = {}
    if isinstance(raw_elems, list):
        for e in raw_elems:
            if not isinstance(e, dict):
                continue
            eid   = str(e.get("id") or "").strip()
            grade = str(e.get("grade") or "").strip()
            if eid in _ELID_TO_NAME and grade in _VALID_GRADES and eid not in by_id:
                by_id[eid] = grade
    # 측정된 요소만, 표준 순서로. 표시명·SRL은 코드가 권위 있게 채운다.
    elements = [
        {"id": eid, "name": _ELID_TO_NAME[eid], "grade": by_id[eid], "srl": eid in _SRL_IDS}
        for eid in _CT_ORDER if eid in by_id
    ]
    return {
        "elements":           elements,
        "narrative_feedback": _clamp_narrative(
            ct_raw.get("narrative_feedback") or ct_raw.get("feedback") or ""),
        "highlights":         _norm_highlights(ct_raw.get("근거") or ct_raw.get("highlights")),
    }


def save_feedback(session_id: str, topic: str, scored: dict) -> None:
    """CT 채점 결과(score_ct 반환값)를 feedback.json에 누적 저장한다(교사·연구용 내부 기록)."""
    try:
        records = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        grades = {e["name"]: e["grade"] for e in scored.get("elements", [])}
        records.append({
            "session_id":         session_id,
            "timestamp":          now,
            "topic":              topic,
            "grades":             grades,
            "narrative_feedback": scored.get("narrative_feedback", ""),
            "highlights":         scored.get("highlights", []),
        })

        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"피드백 저장 오류: {e}")


def _latest_feedback(session_id: str) -> dict | None:
    """주어진 session_id의 가장 최근 피드백을 반환한다. 없으면 None."""
    if not os.path.exists(FEEDBACK_FILE):
        return None
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        matches = [r for r in records if r.get("session_id") == session_id]
        return matches[-1] if matches else None
    except Exception:
        return None


_CJK_RE = re.compile(r"[一-鿿]")   # 한자(중국어) 영역 — 한글(가-힣)은 안 잡힌다.


def _has_cjk(text) -> bool:
    """문자열에 한자(중국어)가 섞였는지. 언어가드(프롬프트)가 가끔 새는 것을 코드로 막는 용도."""
    return bool(_CJK_RE.search(str(text or "")))


def _q_has_cjk(q: dict) -> bool:
    """문항(질문·보기·해설)에 한자가 섞였는지."""
    if _has_cjk(q.get("question")):
        return True
    if any(_has_cjk(o) for o in q.get("options", [])):
        return True
    return _has_cjk(q.get("explanation"))


def _options_ok(opts) -> bool:
    """보기 4개가 정상인지 — 4개·비어있지 않음·서로 다름·한 보기에 여러 보기가 몰리지 않음.
    9B 모델이 가끔 보기 4개를 한 칸에 다 몰아넣고 나머지 칸을 garbage(answer_type… 등)로
    채우는 손상 출력을 감지한다(스키마는 '문자열 4개'만 강제하므로 못 거름)."""
    if not (isinstance(opts, list) and len(opts) == 4):
        return False
    bodies = [str(o).strip() for o in opts]
    if any(not b for b in bodies):
        return False
    if len(set(bodies)) != 4:                      # 손상 시 같은 garbage가 반복됨
        return False
    for o in bodies:
        if "answer_type" in o:                      # 필드명 누출
            return False
        if len(re.findall(r"(?:^|\s)[A-D][.)]\s", o)) >= 2:   # 한 보기 안에 여러 보기 라벨
            return False
    return True


def _option_value(option_text: str, label: str) -> str:
    """'B. 22000' → '22000'. 라벨 다음 구분자(. : )) 와 공백을 벗긴다."""
    text = option_text.strip()
    m = re.match(rf"{re.escape(label)}\s*[.):]?\s*(.*)", text, re.DOTALL)
    return (m.group(1).strip() if m else text)


_NUM_RE       = re.compile(r"-?\d[\d,]*\.?\d*")
_CLEAN_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*$")


def _to_number(s: str):
    """문자열에서 첫 숫자(부호·천단위쉼표·소수점 포함)를 추출해 int/float로. 없으면 None."""
    m = _NUM_RE.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        f = float(m.group().replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def _values_match(actual: str, expected: str) -> bool:
    """실행 stdout과 정답 보기 값의 일치 여부를 표면 차이에 관대하게 판정한다.

    근본 원인 대응: 모델이 보기 값에 단위·접미사(회/개/원/명/번 등)나 따옴표·천단위
    쉼표를 붙여 stdout과 글자 단위로 어긋나도, 가리키는 값이 같으면 정답으로 본다.
    오탐을 막기 위해 숫자 일치는 stdout이 '순수 숫자'일 때만 인정한다."""
    a, e = actual.strip(), expected.strip()
    if a == e:
        return True

    def _unquote(s):
        return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"" else s
    if _unquote(a) == _unquote(e):
        return True

    na, ne = _to_number(a), _to_number(e)
    if na is not None and ne is not None and na == ne and _CLEAN_NUM_RE.fullmatch(a.replace(" ", "")):
        return True
    return False


def _build_verification_program(snippet: str, code: str) -> str:
    """실행할 파이썬 소스를 만든다.

    code가 있으면 원본 프로그램의 함수·전역을 먼저 정의(그 자체 출력은 억제)한 뒤
    스니펫을 같은 전역에서 실행한다. 스니펫이 원본 함수(calculate_stock 등)나 전역을
    재정의 없이 참조해도 NameError가 나지 않게 하는 게 핵심.
    스니펫이 자족적이면(필요한 정의 포함) 재정의가 우선하므로 무해하다."""
    if not code.strip():
        return snippet
    return (
        "import io as _vc_io, contextlib as _vc_ctx\n"
        f"_vc_code = {code!r}\n"
        "_vc_ns = {'__name__': '__main__'}\n"
        "try:\n"
        "    with _vc_ctx.redirect_stdout(_vc_io.StringIO()):\n"
        "        exec(compile(_vc_code, '<code>', 'exec'), _vc_ns)\n"
        "except Exception:\n"
        "    pass  # 원본 실행 실패해도 정의된 함수/전역만 있으면 스니펫이 살 수 있다\n"
        "globals().update({_vc_k: _vc_v for _vc_k, _vc_v in _vc_ns.items() "
        "if not _vc_k.startswith('__')})\n"
        "# ---- verification snippet ----\n"
        f"{snippet}\n"
    )


_STMT_HEAD_RE = re.compile(
    r"^(def |class |import |from |return\b|if |elif |else|for |while |try|except|finally|with |raise |@|pass\b|break\b|continue\b)"
)
_ASSIGN_RE = re.compile(r"^[A-Za-z_][\w.\[\]\"'() ]*\s(?:=|\+=|-=|\*=|/=)[^=]")


def _ensure_snippet_prints(snippet: str) -> str:
    """스니펫에 print(가 하나도 없으면, 마지막 '최상위 표현식' 줄을 print(...)로 감싼다.
    모델이 함수를 호출만 하고 print를 빠뜨리는 흔한 실수(stdout 빈 값 → 검증 실패)를 구제한다.
    들여쓰기된 줄·정의/제어/대입문은 건드리지 않는다(안전 우선, 애매하면 원본 유지)."""
    if "print(" in snippet:
        return snippet
    lines = snippet.rstrip("\n").split("\n")
    idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None)
    if idx is None:
        return snippet
    line = lines[idx]
    stripped = line.strip()
    if line[:1].isspace():                 # 들여쓰기된 줄(함수 본문 등)은 대상 아님
        return snippet
    if _STMT_HEAD_RE.match(stripped) or _ASSIGN_RE.match(stripped):
        return snippet                     # 정의·제어·대입문이면 출력할 표현식이 아님
    lines[idx] = f"print({stripped})"
    return "\n".join(lines)


def _run_verification_snippet(snippet: str, code: str = "") -> str:
    """
    verification_snippet을 임시 .py 파일로 저장해 별도 subprocess로 실행한다.
    code가 주어지면 원본 프로그램의 정의를 먼저 주입한다(_build_verification_program).
    타임아웃 VERIFY_TIMEOUT_SEC초, stdout.strip() 반환. 실패/타임아웃은 raise.
    (subprocess.run이 타임아웃 시 자식을 종료한 뒤 TimeoutExpired를 올린다.)
    """
    program = _build_verification_program(_ensure_snippet_prints(snippet), code)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(program)
        tmp.close()
        result = subprocess.run(
            [sys.executable, tmp.name],
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_SEC,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass  # 타임아웃으로 자식이 잡고 있는 등 드문 경우 — 임시파일만 남고 무해
    if result.returncode != 0:
        raise RuntimeError(f"실행 오류: {result.stderr.strip()[:200]}")
    return (result.stdout or "").strip()


# ══════════════════════════════════════════════════════════════════
# 문항 생성 (question_generation_revision.md) — 전부 4지선다
#   경로 A(결정적, LLM 없음): 유형 1 문제분해
#   경로 B(LLM + verification_snippet 실행 검증): 유형 3·4·5
# ══════════════════════════════════════════════════════════════════

# ── 경로 B 공통: 단일 문항 파싱·검증·실행검증 ──────────────────────


# ── 경로 B 공통: 단일 문항 파싱·검증 + 정답을 실행값으로 확정 ──────────
# 핵심: 9B가 보기 값을 실행 출력과 글자까지 똑같이 못 맞춰 스킵되던 문제를 없앤다.
# 사양 §3 "정답은 반드시 snippet 실행값" 원칙대로, 서버가 정답 보기를 실행값으로 '확정'하고
# 나머지 보기만 서로 다르게 보정한다 → 검증 불일치로 문항이 버려지지 않아 항상 5문항.
def _validate_typed(q: dict) -> None:
    """유형 3·4·5 LLM 문항의 필드·구조 검증(보기 중복은 뒤에서 보정하므로 여기선 허용)."""
    if not isinstance(q, dict):
        raise ValueError("JSON 객체가 아님")
    if not q.get("stem"):
        raise ValueError("stem 없음")
    opts = q.get("options")
    if not (isinstance(opts, list) and len(opts) == 4):
        n = len(opts) if isinstance(opts, list) else "N/A"
        raise ValueError(f"options 4개 필요(현재 {n})")
    ai = q.get("answer_index")
    if not (isinstance(ai, int) and 0 <= ai <= 3):
        raise ValueError(f"answer_index 오류: {ai!r}")
    vs = q.get("verification_snippet")
    if not (isinstance(vs, str) and vs.strip()):
        raise ValueError("verification_snippet 없음")
    fp = q.get("focus_points")
    if not (isinstance(fp, list) and 1 <= len(fp) <= 3
            and all(isinstance(x, str) and x.strip() for x in fp)):
        raise ValueError("focus_points 1~3개 필요")
    if _has_cjk(q.get("stem")) or any(_has_cjk(o) for o in opts) or _has_cjk(q.get("explanation")):
        raise ValueError("중국어(한자) 혼입")


def _parse_typed_question(raw: str) -> dict:
    """유형 3·4·5 단일 문항 JSON 파싱 + 구조 검증."""
    q = json.loads(_slice_first_json(raw), strict=False)
    _validate_typed(q)
    return q


def _value_variants(s: str) -> list:
    """정답값 s와 '다르지만 그럴듯한' 오답 후보들(숫자면 ±, 여러 줄이면 줄 가감)."""
    s = (s or "").strip()
    out = []
    try:
        f = float(s.replace(",", "")); is_num = True
    except ValueError:
        f = None; is_num = False
    if is_num:
        n = int(f) if f.is_integer() else f
        for d in (1, -1, 2, -2, 10, -10):
            v = n + d
            out.append(str(int(v) if isinstance(n, int) else round(v, 2)))
        if n:
            out.append(str(int(n * 2) if isinstance(n, int) else round(n * 2, 2)))
    else:
        lines = s.split("\n")
        if len(lines) > 1:
            out += ["\n".join(lines[:-1]), "\n".join(lines[1:]), s + "\n(끝)", lines[-1], lines[0]]
        out += ["(출력 없음)", s + " ", s + "?"]
    uniq = []
    for v in out:
        if v != s and v not in uniq:
            uniq.append(v)
    return uniq


def _ensure_distinct(options: list, answer_index: int) -> list:
    """answer_index 보기는 고정(실행값). 비었거나 겹치는 나머지 보기를 변형값으로 바꿔 4개를 서로 다르게."""
    correct = str(options[answer_index])
    variants = _value_variants(correct)
    vi, seen = 0, {correct}
    for i in range(len(options)):
        if i == answer_index:
            continue
        o = str(options[i]).strip()
        while (not o) or (o in seen):
            if vi < len(variants):
                o = variants[vi]; vi += 1
            else:
                o = correct + (" " * (i + 1))   # 최후 수단
        seen.add(o)
        options[i] = o
    return options


def _deterministic_fallback(slot: dict, python_code: str) -> dict | None:
    """LLM이 끝내 실패하면 코드 실행 결과로 문항을 결정적으로 만든다(5문항 보장용)."""
    actual = _run_code_stdout(python_code)
    if not actual:
        return None
    lines = actual.split("\n")
    t = slot["type"]
    if t == "mid_output" and len(lines) >= 2:
        k = max(1, (len(lines) + 1) // 2)
        stem = f"위 코드를 실행할 때 처음 {k}줄까지 출력된 내용은?"
        correct, note = "\n".join(lines[:k]), "중간 출력(처음 일부 줄)"
    elif t == "pattern" and len(lines) >= 2:
        k = max(1, len(lines) - 1)
        stem = f"위 코드를 실행할 때 {k}번째로 출력되는 줄은?"
        correct, note = lines[k - 1], "n번째 출력 줄"
    else:
        stem, correct, note = "위 코드를 실행하면 최종 출력은?", actual, "전체 출력"
    options = _ensure_distinct([correct, "", "", ""], 0)
    order = list(range(4)); random.shuffle(order)
    opts = [options[i] for i in order]
    return {
        "type": t, "ct_skill": slot["ct_skill"], "code_kind": slot["code_kind"],
        "answer_type": slot["answer_type"], "stem": stem,
        "options": opts, "answer_index": order.index(0),
        "verification_snippet": "", "explanation": f"코드를 실제로 실행한 {note} 결과입니다.",
        "focus_points": ["코드를 한 줄씩 실행하며 출력되는 순서를 따라가기"],
    }


def _gen_typed_with_retry(slot: dict, python_code: str, templates: str) -> dict | None:
    """슬롯(유형 3·4·5) 문항 1개 생성. LLM이 발문·오답을 만들고, 서버가 정답을 실행값으로 확정한다.
    파싱/실행이 끝내 실패하면 결정적 폴백으로 채워 문항을 스킵하지 않는다(항상 5문항)."""
    kind, tag = slot["type"], slot["ct_skill"]
    for attempt in range(VERIFY_RETRY_LIMIT):
        raw = None
        try:
            raw = llm.call_typed_question_gen(kind, python_code, templates)
            q = _parse_typed_question(raw)
        except Exception as e:
            print(f"[gen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 파싱/검증 실패 — {e}")
            if raw is not None:
                print(f"[gen {tag}] 원본 응답 ↓↓↓\n{raw}\n[gen {tag}] 원본 응답 ↑↑↑")
            continue
        snippet = (q.get("verification_snippet") or "").strip()
        try:
            actual = _run_verification_snippet(snippet, python_code)
        except Exception as e:
            print(f"[gen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 스니펫 실행 실패 — {e}")
            continue
        ai = q["answer_index"]
        q["options"][ai] = actual                       # 정답 = snippet 실행값(§3)
        q["options"] = _ensure_distinct(q["options"], ai)   # 나머지 보기 서로 다르게
        q["ct_skill"]   = slot["ct_skill"]
        q["code_kind"]  = slot["code_kind"]
        q["answer_type"] = slot["answer_type"]
        q["type"]       = slot["type"]
        print(f"[gen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 성공 — 정답={actual[:40]!r}")
        return q
    fb = _deterministic_fallback(slot, python_code)
    print(f"[gen {tag}] LLM {VERIFY_RETRY_LIMIT}회 실패 → "
          + ("결정적 폴백 사용" if fb else "폴백도 실패(스킵)"))
    return fb


_ORDER_LABELS = ["(가)", "(나)", "(다)", "(라)", "(마)", "(바)", "(사)", "(아)"]


def _group_blocks(pseudocode_lines: list) -> list:
    """의사코드 줄을 '단계 블록'으로 묶는다(들여쓰기 기준, 결정적).

    함수 정의 한 줄이 본문 전체를 삼켜 블록이 2개로 줄어드는 문제(대부분의 의사코드가
    '함수 정의 + 들여쓴 본문 + 마지막 호출' 꼴)를 피하려고, 블록을 나누는 기준 들여쓰기 수준을
    고정 0이 아니라 '블록이 3개 이상 나오는 가장 얕은 수준'으로 잡는다.
    그 수준의 줄(과 그보다 얕은 줄)에서 새 블록이 시작되고, 더 깊은 줄(반복·조건의 본문)은 합쳐진다.
    → 'i를 …까지 반복:' + 그 본문이 한 블록으로 유지된다(spec §1: 최상위 단계 + 들여쓰기 하위 = 한 블록)."""
    lines = [str(ln).rstrip() for ln in pseudocode_lines if str(ln).strip()]
    if not lines:
        return []
    indents = sorted({len(ln) - len(ln.lstrip(" ")) for ln in lines})

    def group_at(base: int) -> list:
        blocks = []
        for ln in lines:
            ind = len(ln) - len(ln.lstrip(" "))
            if ind <= base or not blocks:
                blocks.append([ln])
            else:
                blocks[-1].append(ln)
        return blocks

    # base 오름차순으로 블록 수는 단조 증가 → 3개 이상 나오는 가장 얕은 base를 쓴다.
    chosen = group_at(indents[-1])
    for base in indents:
        g = group_at(base)
        if len(g) >= 3:
            chosen = g
            break
    return chosen


def _order_distractors(n: int, count: int = 3) -> list:
    """0..n-1 정답 순서에서 출발한 '틀린 순열' count개(서로 다르고 정답과도 다름).
    인접 교환·한 원소 이동·역순을 우선 쓰고, 부족하면 무작위 순열로 보충."""
    correct = tuple(range(n))
    cands = []
    for i in range(n - 1):
        p = list(range(n)); p[i], p[i + 1] = p[i + 1], p[i]
        cands.append(tuple(p))
    for i in range(n):
        rest = [x for x in range(n) if x != i]
        cands.append((i, *rest))
        cands.append((*rest, i))
    cands.append(tuple(reversed(range(n))))
    seen, out = {correct}, []
    random.shuffle(cands)
    for p in cands:
        if p not in seen:
            seen.add(p); out.append(p)
    guard = 0
    while len(out) < count and guard < 300:
        p = list(range(n)); random.shuffle(p); p = tuple(p)
        if p not in seen:
            seen.add(p); out.append(p)
        guard += 1
    return out[:count]


def _build_decomposition_order(pseudocode_lines) -> dict | None:
    """유형 1: 의사코드 블록을 라벨링·셔플해 '순서 고르기' 4지선다를 코드로 구성한다.
    구조 검증만 — 블록≥3, 보기 4개 서로 다른 순열, 정답 1개. 미달 시 None(스킵)."""
    if not isinstance(pseudocode_lines, list):
        return None
    blocks = _group_blocks(pseudocode_lines)
    n = len(blocks)
    if n < 3 or n > len(_ORDER_LABELS):
        print(f"[type1] 블록 수 {n} — 부적합(3~{len(_ORDER_LABELS)}) → 스킵")
        return None

    display_perm = list(range(n))
    random.shuffle(display_perm)                 # 화면 표시 순서(섞음)
    display_blocks, label_of_orig = [], {}
    for pos, orig in enumerate(display_perm):
        label = _ORDER_LABELS[pos]
        label_of_orig[orig] = label
        display_blocks.append({"label": label, "lines": blocks[orig]})

    correct_str = "-".join(label_of_orig[orig] for orig in range(n))   # 원래 순서 = 정답
    distractors = _order_distractors(n, 3)
    option_strs = [correct_str] + ["-".join(label_of_orig[orig] for orig in perm)
                                   for perm in distractors]
    order = list(range(len(option_strs)))
    random.shuffle(order)
    options = [option_strs[i] for i in order]
    answer_index = order.index(0)

    if len(options) != 4 or len(set(options)) != 4:
        print("[type1] 보기 4개 구성 실패 → 스킵")
        return None

    return {
        "type": "order",
        "ct_skill": "문제분해",
        "code_kind": "pseudocode",
        "answer_type": "order",
        "stem": "위 단계들을 올바른 순서로 나열한 것은?",
        "blocks": display_blocks,
        "options": options,
        "answer_index": answer_index,
        "verification_snippet": "",
        "explanation": "들여쓰기로 묶은 각 단계 블록을 원래(올바른) 순서로 되돌린 배열입니다.",
        "focus_points": ["각 블록이 무슨 일을 하는 단계인지 파악", "먼저 일어나야 하는 단계부터 차례로 나열"],
    }


# ── RAG: 유형 3·4·5만 problem_templates 참조 ───────────────────────
def _retrieve_template_for(slot: dict, code: str) -> str:
    """슬롯에 template 파일이 지정된 경우에만 problem_templates에서 가이드를 가져온다(없으면 "")."""
    src = slot.get("template")
    if not src:
        return ""
    return rag_store.retrieve(
        "problem_templates", f"{slot['ct_skill']} {code[:200]}", k=2,
        filter={"source_file": src},
    ) or ""


# ── 유형 2 추상화/알고리즘 (경로 A — 결정적, LLM 없음): 순서도 고르기 ──
# python_code를 AST로 분석해 구조(반복만 / 반복+조건)를 판별하고, 골격에 실제 조건식·갱신문을
# 채워 정답 순서도(Mermaid)를 만든다. 오답 3개는 정답 그래프의 통제된 변형이다.
def _u(node) -> str:
    """AST 노드를 짧은 소스 텍스트로(순서도 라벨용). 길면 잘라낸다."""
    try:
        s = ast.unparse(node)
    except Exception:
        s = "..."
    s = " ".join(s.split())
    return s[:38] + ("…" if len(s) > 38 else "")


def _mm_label(text: str) -> str:
    """Mermaid 노드 라벨 이스케이프(따옴표로 감싸 쓰므로 큰따옴표·개행만 정리)."""
    return " ".join(str(text).replace('"', "'").split()) or " "


def _mermaid(nodes: list, edges: list) -> str:
    """(nodes, edges) → Mermaid flowchart 문자열.
    nodes: [(id, shape, text)] shape ∈ {term, proc, dec}. edges: [(src, dst, label)]."""
    out = ["flowchart TD"]
    for nid, shape, text in nodes:
        t = _mm_label(text)
        if shape == "term":
            out.append(f'    {nid}(["{t}"])')
        elif shape == "dec":
            out.append(f'    {nid}{{"{t}"}}')
        else:
            out.append(f'    {nid}["{t}"]')
    for src, dst, label in edges:
        out.append(f'    {src} -- {label} --> {dst}' if label else f'    {src} --> {dst}')
    return "\n".join(out)


def _flow_graph(python_code: str):
    """단일 함수를 분석해 정답 순서도의 (nodes, edges, structure)를 만든다.
    structure: 'loop'(반복만) | 'loop_if'(반복+조건). 분석 불가(함수≠1·반복 없음)면 None.
    → 이 그래프가 곧 'AST 구조와 일치하는 정답'이다(구조 검증 = 빌드 자체)."""
    try:
        tree = ast.parse(python_code)
    except SyntaxError:
        return None
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1:
        return None
    body = funcs[0].body
    loop = next((n for n in body if isinstance(n, (ast.For, ast.While))), None)
    if loop is None:
        return None
    li = body.index(loop)
    init_text = "; ".join(_u(s) for s in body[:li]) or "변수 초기화"
    out_text = "; ".join(_u(s) for s in body[li + 1:]) or "결과 반환"
    if isinstance(loop, ast.For):
        cond_text = f"{_u(loop.target)} ← {_u(loop.iter)} (남은 값 있는가?)"
    else:
        cond_text = f"{_u(loop.test)} ?"

    body_if = next((n for n in loop.body if isinstance(n, ast.If)), None)
    if body_if is None:                                   # 반복만
        body_text = "; ".join(_u(s) for s in loop.body) or "반복 본문"
        nodes = [
            ("S", "term", "시작"), ("I", "proc", init_text), ("C", "dec", cond_text),
            ("B", "proc", body_text), ("O", "proc", out_text), ("E", "term", "끝"),
        ]
        edges = [
            ("S", "I", ""), ("I", "C", ""),
            ("C", "B", "예"), ("B", "C", ""),
            ("C", "O", "아니오"), ("O", "E", ""),
        ]
        return (nodes, edges, "loop")
    # 반복+조건
    then_text = "; ".join(_u(s) for s in body_if.body) or "처리"
    nodes = [
        ("S", "term", "시작"), ("I", "proc", init_text), ("C", "dec", cond_text),
        ("Q", "dec", f"{_u(body_if.test)} ?"), ("T", "proc", then_text),
        ("O", "proc", out_text), ("E", "term", "끝"),
    ]
    edges = [
        ("S", "I", ""), ("I", "C", ""),
        ("C", "Q", "예"), ("Q", "T", "예"), ("Q", "C", "아니오"),
        ("T", "C", ""), ("C", "O", "아니오"), ("O", "E", ""),
    ]
    return (nodes, edges, "loop_if")


def _mut_swap_yesno(edges: list) -> list:
    """조건(C)의 예/아니오 라벨을 뒤바꾼다(반복 종료 조건을 거꾸로 만든 오답)."""
    out = []
    for src, dst, label in edges:
        if src == "C" and label == "예":
            label = "아니오"
        elif src == "C" and label == "아니오":
            label = "예"
        out.append((src, dst, label))
    return out


def _mut_remove_loopback(edges: list) -> list:
    """반복 복귀(본문 → 조건) 화살표를 제거(출력으로 보내 '한 번만 실행'되는 오답)."""
    back_src = "T" if any(s == "T" and d == "C" for s, d, _ in edges) else "B"
    return [((src, "O", lbl) if (src == back_src and dst == "C" and lbl == "") else (src, dst, lbl))
            for src, dst, lbl in edges]


def _mut_swap_init(edges: list) -> list:
    """초기화·반복 순서를 뒤바꾼다(초기화가 반복 안으로 들어간 오답): S→C 먼저, 초기화는 '예' 분기 안으로."""
    yes_target = next((d for s, d, l in edges if s == "C" and l == "예"), "B")
    out = [("S", "C", "")]
    for src, dst, lbl in edges:
        if (src, dst) in (("S", "I"), ("I", "C")):
            continue
        if src == "C" and lbl == "예":
            out.append(("C", "I", "예"))
            continue
        out.append((src, dst, lbl))
    out.append(("I", yes_target, ""))
    return out


def _build_flowchart_question(python_code: str) -> dict | None:
    """유형 2: AST→순서도 골격→Mermaid 정답 + 통제 변형 오답 3개. 구조 검증: 보기 4개 서로 다름."""
    g = _flow_graph(python_code)
    if g is None:
        print("[type2] 코드 구조 분석 실패(함수≠1 또는 반복 없음) → 스킵")
        return None
    nodes, edges, _structure = g
    option_strs = [_mermaid(nodes, edges)]                      # 정답
    for mut in (_mut_swap_yesno, _mut_remove_loopback, _mut_swap_init):
        m = _mermaid(nodes, mut(edges))
        if m not in option_strs:
            option_strs.append(m)
    if len(option_strs) != 4 or len(set(option_strs)) != 4:
        print(f"[type2] 보기 변형 중복(distinct={len(set(option_strs))}) → 스킵")
        return None
    order = list(range(4))
    random.shuffle(order)
    options = [option_strs[i] for i in order]
    answer_index = order.index(0)
    return {
        "type": "diagram",
        "ct_skill": "추상화/알고리즘",
        "code_kind": "pseudocode",
        "answer_type": "diagram",
        "stem": "위 의사코드를 올바르게 나타낸 순서도는?",
        "options": options,
        "answer_index": answer_index,
        "verification_snippet": "",
        "explanation": "초기화 → 조건 검사 → (참이면) 본문 실행 후 다시 조건으로 돌아가는 반복 구조를 바르게 나타낸 순서도입니다.",
        "focus_points": ["조건이 참/거짓일 때 흐름이 어디로 가는지", "반복 본문 뒤 다시 조건으로 돌아가는 화살표가 있는지"],
    }


def _build_question_set(python_code: str, pseudocode_lines) -> list:
    """슬롯 순서대로 문항을 만든다(유형1·2 결정적 + 유형3·4·5 LLM+검증). 실패 슬롯은 스킵."""
    questions = []
    for slot in PROBLEM_SLOTS:
        if slot["type"] == "order":
            q = _build_decomposition_order(pseudocode_lines)
        elif slot["type"] == "diagram":
            q = _build_flowchart_question(python_code)
        else:
            q = _gen_typed_with_retry(slot, python_code, _retrieve_template_for(slot, python_code))
        if q:
            questions.append(q)
    return questions


def _student_safe_questions(questions: list) -> list:
    """학생 응답용 문항 필드만 추린다.
    내부 전용 필드(verification_snippet·focus_points·answer_type)는 제외(§7).
    answer_index → 라벨(A~D)로 변환해 기존 클라이언트 채점(selectedOption===answer)과 호환한다.
    유형1(order)은 코드 섹션 표시용 blocks(라벨+줄, 섞인 순서)도 함께 보낸다."""
    safe = []
    for q in questions:
        item = {
            "ct_skill":    q.get("ct_skill"),
            "code_kind":   q.get("code_kind"),       # 프론트 코드 패널 렌더 분기
            "question":    q.get("stem"),
            "options":     q.get("options"),
            "answer":      chr(65 + int(q.get("answer_index", 0))),
            "explanation": q.get("explanation", ""),
            "type":        q.get("type"),
        }
        if q.get("type") == "order":
            item["blocks"] = q.get("blocks")
        safe.append(item)
    return safe


def _save_problem_set(session_id: str, questions: list) -> None:
    """문항 세트(focus_points 포함 full 버전)를 session_id로 서버에 보관한다.
    학생 전송본과 같은 순서·길이(검증/스킵 반영 후)이므로 problem_index가 그대로 맞는다."""
    if not session_id:
        return
    try:
        store = {}
        if os.path.exists(PROBLEM_SETS_FILE):
            with open(PROBLEM_SETS_FILE, "r", encoding="utf-8") as f:
                store = json.load(f)
        store[session_id] = {
            "stored_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "questions": questions,
        }
        with open(PROBLEM_SETS_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"문항 세트 저장 오류: {e}")


def _get_stored_question(session_id: str, index) -> dict | None:
    """보관된 세트에서 index번째 문항(full)을 반환한다. 없으면 None."""
    if not session_id or index is None:
        return None
    try:
        index = int(index)
    except (ValueError, TypeError):
        return None
    try:
        if not os.path.exists(PROBLEM_SETS_FILE):
            return None
        with open(PROBLEM_SETS_FILE, "r", encoding="utf-8") as f:
            store = json.load(f)
        questions = store.get(session_id, {}).get("questions", [])
        if 0 <= index < len(questions):
            return questions[index]
    except Exception as e:
        print(f"문항 세트 조회 오류: {e}")
    return None


def _code_line_count(code: str) -> int:
    """코드 길이를 줄 수로 센다(빈 줄 제외 — 실제 코드 분량 기준)."""
    return sum(1 for ln in code.split("\n") if ln.strip())


# 코드 본문에 섞여 나오는 마크다운 구분선(--- *** ___ ```)은 파이썬에선 무효라 제거한다.
_MD_LINE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,}|`{3,}[a-zA-Z]*)\s*$")
# _CJK_RE는 상단(보기·문항 한자 검사)에서 정의됨 — 재사용


def _clean_code(code: str) -> str:
    """코드 사이에 낀 마크다운 구분선 등 비파이썬 잔재를 제거한다."""
    return "\n".join(ln for ln in code.split("\n") if not _MD_LINE_RE.match(ln)).strip()


def _validate_python_code(code: str) -> tuple:
    """AST 검증: 구문 정상 / 함수 정확히 1개 / 그 함수 안 반복문>=1 / import·input() 없음. (ok, why).
    input()은 헤드리스 실행(검증·중간출력)을 막으므로 구조 단계에서 거른다(데이터는 코드에 고정해야 함)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return (False, f"구문 오류({e.msg})")
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1:
        return (False, f"함수 {len(funcs)}개(정확히 1개 필요)")
    loops = [n for n in ast.walk(funcs[0]) if isinstance(n, (ast.For, ast.While))]
    if not loops:
        return (False, "함수 내부 반복문 없음")
    if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
        return (False, "import 사용")
    if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "input"
           for n in ast.walk(tree)):
        return (False, "input() 사용")
    return (True, "")


def _run_code_stdout(code: str):
    """코드를 격리 실행해 stdout(strip) 반환. 실패·타임아웃이면 None."""
    try:
        return _run_verification_snippet(code, "")
    except Exception:
        return None


def _gen_validated_code(topic: str, ctx: str, difficulty: str) -> str:
    """코드 생성 + 검증(AST 구조 / 줄수 / 한자 / 실행 stdout>=2줄).
    하나라도 실패하면 최대 CODE_GEN_RETRY회 재생성. 전부 실패하면 최선 후보."""
    best = None
    for attempt in range(CODE_GEN_RETRY):
        try:
            raw = llm.call_code_gen(topic=topic, ctx=ctx, difficulty=difficulty)
            code = _clean_code(strip_fences(
                json.loads(_slice_first_json(raw), strict=False).get("python_code", "")))
        except Exception as e:
            print(f"[code-gen] {attempt+1}/{CODE_GEN_RETRY} 파싱 실패 — {e}")
            continue
        if not code:
            continue
        n = _code_line_count(code)
        has_cjk = bool(_CJK_RE.search(code))
        struct_ok, why = _validate_python_code(code)
        stdout = _run_code_stdout(code)
        out_lines = len(stdout.split("\n")) if stdout else 0
        if struct_ok and n <= CODE_MAX_LINES and not has_cjk and out_lines >= 2:
            return code
        score = ((0 if struct_ok else 4000) + (1000 if has_cjk else 0)
                 + (0 if out_lines >= 2 else 2000) + n)
        if best is None or score < best[1]:
            best = (code, score)
        reasons = []
        if not struct_ok: reasons.append(why)
        if n > CODE_MAX_LINES: reasons.append(f"{n}줄>{CODE_MAX_LINES}")
        if has_cjk: reasons.append("한자혼입")
        if out_lines < 2: reasons.append(f"출력 {out_lines}줄(<2)")
        print(f"[code-gen] {attempt+1}/{CODE_GEN_RETRY} 재생성 — {', '.join(reasons)}")
    print("[code-gen] 제한 내 실패 → 최선 후보 사용")
    return best[0] if best else ""


def _gen_pseudocode(python_code: str) -> list:
    """python_code를 한글 의사코드 줄 배열로 번역. 실패 시 ≤CODE_GEN_RETRY회 재생성, 끝내 실패하면 []."""
    for attempt in range(CODE_GEN_RETRY):
        try:
            raw = llm.call_pseudocode_gen(python_code)
            lines = json.loads(_slice_first_json(raw), strict=False).get("pseudocode_lines")
        except Exception as e:
            print(f"[pseudocode] {attempt+1}/{CODE_GEN_RETRY} 파싱 실패 — {e}")
            continue
        if (isinstance(lines, list) and len(lines) >= 3
                and all(isinstance(x, str) and x.strip() for x in lines)
                and not any(_has_cjk(x) for x in lines)
                and any(k in " ".join(lines) for k in ("반복", "만약", "반환", "출력", "정의"))):
            return [x.rstrip() for x in lines]
        print(f"[pseudocode] {attempt+1}/{CODE_GEN_RETRY} 구조 미달 — 재생성")
    print("[pseudocode] 실패 → 빈 배열(유형1 스킵)")
    return []


@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)
    ctx = rag_store.retrieve("code_examples", topic)
    try:
        python_code = _gen_validated_code(topic, ctx, TARGET_DIFFICULTY)
        if not python_code:
            return jsonify({"error": "코드 생성 실패"}), 503
        pseudocode_lines = _gen_pseudocode(python_code)
    except Exception as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({
        "code": python_code,            # 하위호환 별칭
        "python_code": python_code,
        "pseudocode_lines": pseudocode_lines,
        "topic": topic,
    })


@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    """코드 제시 자산(python_code, pseudocode_lines)으로 4지선다 문항 세트를 생성한다."""
    data = request.get_json() or {}
    python_code = data.get("python_code") or data.get("code", "")
    if not python_code:
        return jsonify({"error": "python_code(또는 code) 필드 필요"}), 400
    pseudocode_lines = data.get("pseudocode_lines") or []
    session_id = (data.get("session_id") or "").strip()

    questions = _build_question_set(python_code, pseudocode_lines)
    if not questions:
        return jsonify({"error": "생성된 문제가 없습니다."}), 503
    _save_problem_set(session_id, questions)   # 내부 보관(focus_points 포함)
    return jsonify({"questions": _student_safe_questions(questions)})


def _gen_session_id() -> str:
    return f"{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}"


@api.route("/session/start", methods=["POST"])
def session_start():
    """한 번의 호출로 코드 자산(python_code·pseudocode_lines) + 문항 세트를 생성·반환한다."""
    data = request.get_json(silent=True) or {}
    topic_hint = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)

    code_ctx = rag_store.retrieve("code_examples", topic_hint)
    try:
        python_code = _gen_validated_code(topic_hint, code_ctx, TARGET_DIFFICULTY)
        if not python_code:
            return jsonify({"error": "코드 생성 실패"}), 503
        pseudocode_lines = _gen_pseudocode(python_code)
    except Exception as e:
        return jsonify({"error": f"코드 생성 실패: {e}"}), 503

    questions = _build_question_set(python_code, pseudocode_lines)
    if not questions:
        return jsonify({"error": "문제 생성 실패"}), 503
    session_id = _gen_session_id()
    _save_problem_set(session_id, questions)
    return jsonify({
        "session_id":       session_id,
        "topic":            topic_hint,
        "code":             python_code,
        "python_code":      python_code,
        "pseudocode_lines": pseudocode_lines,
        "questions":        _student_safe_questions(questions),
    })


@api.route("/rag/status", methods=["GET"])
def rag_status():
    return jsonify({
        "available": rag_store._AVAILABLE,
        "embed_model": rag_store.EMBED_MODEL,
        "docs_dir": rag_store.DOCS_DIR,
        "counts": rag_store.counts(),
        "files": rag_store.list_docs(),
    })


@api.route("/rag/rebuild", methods=["POST"])
def rag_rebuild():
    data = request.get_json(silent=True) or {}
    collection = data.get("collection")

    if collection and collection not in rag_store.COLLECTIONS:
        return jsonify({"error": f"collection은 {list(rag_store.COLLECTIONS)} 중 하나여야 합니다."}), 400

    try:
        result = rag_store.rebuild(collection)
        return jsonify({"ok": True, "counts": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@api.route("/chat", methods=["POST"])
def chat():
    """
    챗봇 스트리밍 엔드포인트.
      - trigger == "hint" / "explain"  : 챗봇이 먼저 말함 (history 무시)
      - trigger 없음                   : history 기반 일반 응답
    """
    data = request.get_json()
    trigger = data.get("trigger")
    history = data.get("messages", [])
    session_id = data.get("session_id", "unknown")
    code_context = data.get("code_context")
    current_problem = data.get("current_problem")
    problem_index = data.get("problem_index")

    # ct_skill·focus_points는 서버 보관 세트에서만 가져온다 (클라이언트발 focus_points 불신 — 노출 차단).
    ct_skill = data.get("ct_skill")   # 비민감 메타. 보관 세트가 있으면 그 값으로 덮어씀.
    focus_points = None
    stored_q = _get_stored_question(session_id, problem_index)
    if stored_q:
        ct_skill = stored_q.get("ct_skill") or ct_skill
        focus_points = stored_q.get("focus_points")

    system_prompt = llm.build_chat_system(
        code_context, current_problem, ct_skill, focus_points
    )

    if trigger in ("hint", "explain"):
        trigger_msg = llm.build_trigger_user_message(trigger, ct_skill)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": trigger_msg},
        ]
        user_message_for_log = f"[{trigger} 트리거]"
    else:
        # 대화가 길어지면 모델 컨텍스트를 초과하므로 최근 메시지만 모델에 넣는다.
        recent = history[-MAX_CHAT_HISTORY:]
        messages = [{"role": "system", "content": system_prompt}] + recent
        user_message_for_log = history[-1]["content"] if history else ""

    visible_parts = []

    def generate():
        # 사고는 llm 쪽 </think> 프리필로 차단된다. 이 필터는 혹시 새는 사고 블록을
        # 학생 화면에 내보내지 않기 위한 스트림 방어망이다.
        think_filter = llm.ThinkStreamFilter()
        started = [False]   # 첫 비공백 전까지의 선행 공백(주로 think 제거 후 남는 빈 줄)은 버린다

        def emit(text):
            if not started[0]:
                text = text.lstrip()   # 답변 머리의 빈 줄·공백 제거 → 빈칸 방지
                if not text:
                    return None
                started[0] = True
            visible_parts.append(text)
            return f"data: {json.dumps({'delta': text})}\n\n"

        try:
            stream = llm.stream_chatbot(messages)
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if not delta:
                    continue
                visible = think_filter.feed(delta)
                if visible:
                    msg = emit(visible)
                    if msg:
                        yield msg
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        tail = think_filter.flush()
        if tail:
            msg = emit(tail)
            if msg:
                yield msg

        # 저장·재전송 모두 think 제거·양끝 공백 정리된 본문으로 통일 (로그·후속 맥락 오염 방지)
        full_reply = "".join(visible_parts).strip()
        save_turn(session_id, user_message_for_log, full_reply, code_context)
        yield f"data: {json.dumps({'done': True})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@api.route("/hint-followup", methods=["POST"])
def hint_followup():
    """힌트 후 학생 발화 판정.
      - on_track=True  : 학생 생각이 정답 방향에 맞음 → 추가 응답 없음(대화 종료).
      - on_track=False : 오해·오답 → 스스로 바로잡도록 돕는 유도 질문 1개를 reply로.
    정답·해설·핵심 단서는 서버 보관 세트에서만 가져와 판단 기준으로만 쓰고 노출하지 않는다.
    """
    data = request.get_json() or {}
    session_id      = data.get("session_id", "unknown")
    problem_index   = data.get("problem_index")
    code            = data.get("code_context") or ""
    current_problem = data.get("current_problem") or ""
    history         = data.get("messages", [])

    answer = explanation = ""
    focus_points = None
    stored_q = _get_stored_question(session_id, problem_index)
    if stored_q:
        answer       = stored_q.get("answer", "")
        explanation  = stored_q.get("explanation", "")
        focus_points = stored_q.get("focus_points")

    tail = history[-6:]
    transcript = "\n".join(
        f"{'학생' if m.get('role') == 'user' else 'AI'}: {(m.get('content') or '').strip()}"
        for m in tail if (m.get('content') or '').strip()
    )

    try:
        raw = llm.call_hint_followup(code, current_problem, answer, explanation,
                                     focus_points, transcript)
        parsed = _extract_json(raw)
        on_track = bool(parsed.get("on_track"))
        reply = "" if on_track else (parsed.get("reply") or "").strip()

        last_user = next((m.get("content", "") for m in reversed(history)
                          if m.get("role") == "user"), "")
        save_turn(session_id, last_user,
                  reply if reply else "[on_track — 추가 응답 없음]", code)
        return jsonify({"on_track": on_track, "reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@api.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.get_json()
    topic       = data.get("topic", "")
    code        = data.get("code", "")
    problems    = data.get("problems", [])
    answers     = data.get("answers", [])
    chat_history = data.get("chat_history", [])
    session_id  = data.get("session_id", "unknown")

    # 대화 로그 직렬화 — 분석 대상 자료, messages 맥락 아님.
    # 루브릭은 '학생 발화'만 채점한다. 단 AI 발화도 대화 맥락(어떤 힌트·질문에 학생이
    # 어떻게 반응했는지)을 보려면 필요하므로, 줄마다 역할을 명시해 함께 넣되
    # '학생' 줄만 채점 대상, 'AI' 줄은 맥락(채점 제외)임을 라벨로 분명히 한다.
    # 문제/답안 덤프는 채점에 불필요하므로 넣지 않는다(토큰 절약).
    lines = []
    for msg in chat_history:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if msg.get("role") == "user":
            lines.append(f"[학생·채점대상] {content}")
        else:
            lines.append(f"[AI·맥락(채점제외)] {content}")
    chat_text = "\n".join(lines)
    if len(chat_text) > CHAT_LOG_CHAR_BUDGET:
        # 뒤쪽(최근)을 보존하고 앞부분을 잘라낸다 — 세션의 전형적 수준 평정에 최근이 대표적.
        chat_text = "…(앞부분 생략)…\n" + chat_text[-CHAT_LOG_CHAR_BUDGET:]
    log_text = (
        f"학습 주제: {topic}\n\n"
        f"학생이 읽은 코드:\n{code}\n\n"
        "챗봇 대화 — '[학생·채점대상]' 줄만 컴퓨팅 사고력 채점의 근거로 삼아라. "
        "'[AI·맥락(채점제외)]' 줄은 대화 맥락 파악용일 뿐, 그 내용으로 점수를 매기지 마라:\n"
        f"{chat_text if chat_text else '(대화 없음)'}"
    )

    try:
        ct_raw = _extract_json(llm.call_log_analysis(log_text))

        # 루브릭(prompts/ct_evaluation_rubric.md) 7요소 상/중/하/NA + 서술 피드백
        scored = score_ct(ct_raw)
        save_feedback(session_id, topic, scored)

        # 학생 화면: 숫자 총점 없이 7요소 배지(elements) + 서술 피드백 + 평가 근거(highlights).
        return jsonify({
            "elements":           scored["elements"],
            "narrative_feedback": scored["narrative_feedback"],
            "highlights":         scored["highlights"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 503


def save_turn(session_id, user_message, assistant_reply, code_context=None):
    try:
        sessions = []
        if os.path.exists(CONVERSATIONS_FILE):
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                sessions = json.load(f)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        turn = {"turn_index": None, "timestamp": now, "user": user_message, "assistant": assistant_reply}

        session = next((s for s in sessions if s["session_id"] == session_id), None)
        if session:
            turn["turn_index"] = len(session["turns"]) + 1
            session["turns"].append(turn)
            session["updated_at"] = now
        else:
            turn["turn_index"] = 1
            sessions.append({
                "session_id": session_id,
                "started_at": now,
                "updated_at": now,
                "code_context": code_context,
                "turns": [turn],
            })

        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"대화 저장 오류: {e}")
