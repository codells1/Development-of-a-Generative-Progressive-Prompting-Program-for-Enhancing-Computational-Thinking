from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import random
import re
import os
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

# 문제 출제용 5유형 (분석용 CT 루브릭 지표와 별개). 순서 고정 — 분해→통합.
PROBLEM_CT_SKILLS = ("분해", "패턴인식", "추상화", "알고리즘적사고", "통합")

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


def _validate_question(q: dict, label: str = "문항") -> None:
    """문항 1개의 필드·구조를 검증한다. 실패 시 ValueError.
    세트 생성(_parse_problem_set)과 단일 재생성(_parse_single_problem)이 공유하는
    단일 검증 경로 — 두 경로가 절대 어긋나지 않게 한다."""
    if not isinstance(q, dict):
        raise ValueError(f"{label}: JSON 객체가 아님")
    if not q.get("question"):
        raise ValueError(f"{label}: question 필드 없음")
    opts = q.get("options", [])
    if not (isinstance(opts, list) and len(opts) == 4):
        n = len(opts) if isinstance(opts, list) else "N/A"
        raise ValueError(f"{label}: options 4개 필요 (현재 {n}개)")
    if q.get("answer") not in ("A", "B", "C", "D"):
        raise ValueError(f"{label}: answer 라벨 오류: {q.get('answer')!r}")
    atype = q.get("answer_type")
    if atype not in ("computational", "conceptual"):
        raise ValueError(f"{label}: answer_type 오류: {atype!r}")
    vs = q.get("verification_snippet")
    if not isinstance(vs, str):
        raise ValueError(f"{label}: verification_snippet 필드 없음/타입 오류")
    if atype == "computational" and not vs.strip():
        raise ValueError(f"{label}: computational 문항은 verification_snippet 필수")
    if atype == "conceptual" and vs.strip():
        raise ValueError(f"{label}: conceptual 문항은 verification_snippet 빈 문자열이어야 함")
    fp = q.get("focus_points")
    if not isinstance(fp, list) or not (1 <= len(fp) <= 3):
        n = len(fp) if isinstance(fp, list) else "N/A"
        raise ValueError(f"{label}: focus_points 1~3개 배열 필요 (현재 {n})")
    if not all(isinstance(x, str) and x.strip() for x in fp):
        raise ValueError(f"{label}: focus_points 각 항목은 비어있지 않은 문자열이어야 함")


def _parse_problem_set(raw: str) -> dict:
    """문제 세트 JSON 파싱 + 구조 검증. {title, summary, questions} 반환. 실패 시 ValueError."""
    data = json.loads(_slice_first_json(raw), strict=False)
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) != 5:
        n = len(questions) if isinstance(questions, list) else "N/A"
        raise ValueError(f"questions 5개 필요 (현재 {n})")

    for i, q in enumerate(questions):
        _validate_question(q, f"q{i}")

    # ct_skill 구성·순서(분해→통합, 각 1개)는 여기서 하드 실패시키지 않는다.
    # 모델이 한 유형을 중복하거나 누락해도 _reconcile_skills가 보정·보충하므로
    # 세트 전체를 버리고 재시도(503)하던 근본 원인을 제거한다.
    return {
        "title":     (data.get("title") or "").strip(),
        "summary":   (data.get("summary") or "").strip(),
        "questions": questions,
    }


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


def _verify_one(q: dict, code: str = "") -> tuple:
    """
    단일 computational 문항 실행 검증. (ok: bool, detail: str) 반환.
    conceptual·빈 스니펫은 ok=True.
    스니펫(=정답값의 근거)을 신뢰한다: 출력값이 정답 라벨 보기와 다르더라도
    정확히 한 보기와 일치하면 그 라벨로 정답을 교정해 살린다(모델의 라벨 실수 구제).
    어느 보기와도 안 맞을 때만 실패 → 재생성/스킵.
    code: 원본 프로그램. 스니펫이 그 함수·전역을 참조할 수 있도록 주입한다.
    """
    if q.get("answer_type") != "computational":
        return (True, "skip (conceptual)")
    snippet = (q.get("verification_snippet") or "").strip()
    if not snippet:
        return (True, "skip (empty)")
    options = q.get("options", [])
    ans_label = q.get("answer", "")
    ans_option = next((o for o in options if o.strip().startswith(ans_label)), "")
    expected = _option_value(ans_option, ans_label)
    try:
        actual = _run_verification_snippet(snippet, code)
    except subprocess.TimeoutExpired:
        return (False, f"타임아웃 ({VERIFY_TIMEOUT_SEC}s)")
    except Exception as e:
        return (False, f"실행 오류 — {e}")
    if _values_match(actual, expected):
        return (True, f"일치 {actual!r}")

    # 라벨이 틀렸을 수 있다 — 스니펫 출력과 일치하는 보기가 정확히 하나면 정답 라벨을 교정.
    matches = [o.strip()[0] for o in options
               if o.strip()[:1] in ("A", "B", "C", "D")
               and _values_match(actual, _option_value(o, o.strip()[0]))]
    if len(matches) == 1:
        old = q.get("answer")
        q["answer"] = matches[0]
        return (True, f"정답 라벨 교정 {old}→{matches[0]} (스니펫 출력 {actual!r})")
    return (False, f"불일치 stdout={actual!r}, 정답 보기={expected!r}")


def _parse_single_problem(raw: str, expected_ct_skill: str = None) -> dict:
    """단일 문항 JSON 파싱 + 검증. 재생성 응답용 — 세트 생성과 동일한 검증 경로(_validate_question)를 탄다."""
    q = json.loads(_slice_first_json(raw), strict=False)
    _validate_question(q, "재생성 문항")
    if expected_ct_skill:
        q["ct_skill"] = expected_ct_skill   # 시스템이 요구한 CT 요소로 강제 정렬
    return q


def _regenerate_question(ct_skill: str, code: str, templates: str, log_label: str = "") -> dict | None:
    """지정 CT 유형 문항 1개를 단일 생성·검증한다. 최대 VERIFY_RETRY_LIMIT회 시도.
    검증 통과 문항을 반환하고, 전부 실패하면 None. (검증 실패 문항 재생성·유형 보충 공용)"""
    tag = f"{log_label}{ct_skill}"
    for attempt in range(VERIFY_RETRY_LIMIT):
        raw = None
        try:
            raw = llm.call_single_problem_gen(code, ct_skill, templates)
            new_q = _parse_single_problem(raw, expected_ct_skill=ct_skill)
        except Exception as e:
            print(f"[regen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 파싱 실패 — {e}")
            # 파싱 실패 시 원본 LLM 응답을 통째로 로그로 남긴다 (원인 추적용).
            if raw is not None:
                print(f"[regen {tag}] 원본 응답 ↓↓↓\n{raw}\n[regen {tag}] 원본 응답 ↑↑↑")
            continue
        ok, detail = _verify_one(new_q, code)
        if ok:
            print(f"[regen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 성공 — {detail}")
            return new_q
        print(f"[regen {tag}] {attempt+1}/{VERIFY_RETRY_LIMIT} 검증 실패 — {detail}")
    return None


def _reconcile_skills(questions: list, code: str, templates: str) -> list:
    """문항들을 기대 CT 유형 구성·순서(PROBLEM_CT_SKILLS: 분해→통합, 각 1개)에 맞춘다.

    모델이 한 유형을 중복하거나 누락해도(예: 통합 누락·패턴인식 중복) 세트를 버리지 않고:
      - 각 기대 유형에 해당하는 첫 문항을 순서대로 배치(잉여 중복은 버림)
      - 누락된 유형은 단일 생성으로 보충
    이 함수가 'ct_skill 순서 오류' 503의 근본 해결책이다."""
    by_skill = {}
    for q in questions:
        by_skill.setdefault(q.get("ct_skill"), []).append(q)

    present = tuple(q.get("ct_skill") for q in questions)
    if present != PROBLEM_CT_SKILLS:
        print(f"[reconcile] ct_skill 구성 보정 — 현재 {present} → 기대 {PROBLEM_CT_SKILLS}")

    result = []
    for skill in PROBLEM_CT_SKILLS:
        bucket = by_skill.get(skill)
        if bucket:
            q = bucket.pop(0)
            q["ct_skill"] = skill   # 라벨 정규화
            result.append(q)
        else:
            print(f"[reconcile] '{skill}' 유형 누락 — 단일 생성으로 보충")
            gen = _regenerate_question(skill, code, templates, log_label="reconcile ")
            if gen is not None:
                result.append(gen)
            else:
                print(f"[reconcile] '{skill}' 보충 실패 → 해당 유형 제외")
    return result


def _process_questions(questions: list, code: str, templates: str) -> list:
    """
    spec §3.4 — 검증 실패한 computational 문항만 최대 VERIFY_RETRY_LIMIT회 재생성.
    재생성도 모두 실패하면 그 문항을 결과에서 제외(skip, 정답 보기 덮어쓰지 않음).
    conceptual은 그대로 통과.
    """
    out = []
    for i, q in enumerate(questions):
        ok, detail = _verify_one(q, code)
        if ok:
            out.append(q)
            continue

        ct_skill = q.get("ct_skill", "")
        print(f"[verify q{i} {ct_skill}] 초기 실패 — {detail}")
        replaced = _regenerate_question(ct_skill, code, templates, log_label=f"q{i} ")
        if replaced is not None:
            out.append(replaced)
        else:
            print(f"[skip q{i} {ct_skill}] 재생성 {VERIFY_RETRY_LIMIT}회 실패 → 문항 제외")
            # 스킵: out에 추가하지 않음. 정답은 절대 덮어쓰지 않음.
    return out


# ══════════════════════════════════════════════════════════════════
# 코드 트랙 — '실행 순서 고르기' (quiz_spec.md §4.A 우선순위 1)
# 원칙: 정답·보기를 LLM이 아니라 '실행 트레이스 + 코드'로 결정(100% 결정적).
# 알고리즘적사고 슬롯을 교체하며, 실패 시 기존 코드 문항으로 폴백(회귀 0).
# 기존 _verify_one(스니펫 실행검증)은 건드리지 않는다(answer_type='conceptual'로 우회).
# ══════════════════════════════════════════════════════════════════

EXEC_TRACE_TIMEOUT = 2          # 트레이스 실행 타임아웃(초) — quiz_spec §8
_CIRCLED = "①②③④⑤⑥⑦⑧"

# settrace로 <usercode> 프레임의 'line' 이벤트 줄번호만 순서대로 수집하는 러너.
_TRACER_RUNNER = (
    "import sys, json, io, contextlib\n"
    "_USERCODE = {code!r}\n"
    "_seq = []\n"
    "def _tr(frame, event, arg):\n"
    "    if frame.f_code.co_filename == '<usercode>':\n"
    "        if event == 'line':\n"
    "            _seq.append(frame.f_lineno)\n"
    "        return _tr\n"
    "    return None\n"
    "try:\n"
    "    _compiled = compile(_USERCODE, '<usercode>', 'exec')\n"
    "    sys.settrace(_tr)\n"
    "    with contextlib.redirect_stdout(io.StringIO()):\n"
    "        exec(_compiled, {{'__name__': '__main__'}})\n"
    "except Exception as _e:\n"
    "    sys.settrace(None)\n"
    "    print('TRACE_ERROR:' + repr(_e))\n"
    "    sys.exit(2)\n"
    "finally:\n"
    "    sys.settrace(None)\n"
    "print(json.dumps(_seq))\n"
)


def _run_tracer(code: str):
    """code를 격리 subprocess에서 settrace로 실행해 줄번호 실행 순서(list[int])를 반환.
    실패·타임아웃·런타임에러면 None. (_run_verification_snippet과 같은 격리·타임아웃 패턴)"""
    program = _TRACER_RUNNER.format(code=code)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(program)
        tmp.close()
        result = subprocess.run(
            [sys.executable, tmp.name], capture_output=True, text=True,
            timeout=EXEC_TRACE_TIMEOUT, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if result.returncode != 0:
        return None
    last = (result.stdout or "").strip().splitlines()
    if not last:
        return None
    try:
        seq = json.loads(last[-1])
    except (ValueError, IndexError):
        return None
    return seq if isinstance(seq, list) and all(isinstance(x, int) for x in seq) else None


def _labelable_linenos(code: str) -> set:
    """라벨 가능한 줄 = AST상 '실행문(statement)'의 시작 줄.
    함수/클래스 정의·import는 제외. 다중행 데이터 리터럴(list/dict의 항목 줄 등)은
    한 statement의 일부라 시작 줄 하나만 들어가므로, 데이터 나열이 라벨을 잠식하지 않는다."""
    import ast
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    skip = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    return {node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.stmt) and not isinstance(node, skip)}


# 실행 순서 고르기는 '처음 실행되는 순서'(앞 N개 실행문의 first-occurrence 순열)로 출제한다.
# 실제 생성 코드는 함수+루프로 전체 트레이스가 수십 스텝이라 MCQ에 부적합하므로,
# 함수 정의보다 호출이 먼저 실행되는 등 '줄 번호 순서 ≠ 실행 순서'를 묻는 순열 문항으로 한다.
_EXEC_PICK = 4   # 보기에 쓸 줄 개수(처음 실행되는 앞 N개)


def _first_exec_order(trace: list, label_lines: set) -> list:
    """라벨 대상 줄을 '처음 실행되는' 순서로 중복 없이 나열한 줄번호 리스트."""
    seen, order = set(), []
    for ln in trace:
        if ln in label_lines and ln not in seen:
            seen.add(ln)
            order.append(ln)
    return order


def _perm_distractors(answer: tuple) -> list:
    """순열 정답에서 두 위치를 바꾼 오답 순열들(정답과 상이·상호 유일)."""
    a = list(answer)
    seen, out = {tuple(answer)}, []
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            m = a[:]
            m[i], m[j] = m[j], m[i]
            t = tuple(m)
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _build_execution_order(code: str):
    """실행 순서 고르기 문항 1개. 트레이스→처음 실행 순서→라벨→오답→셔플.
    줄번호 순서와 실행 순서가 같거나(너무 쉬움) 부적합하면 None(→ 코드형 폴백)."""
    code = _clean_code(code)         # 코드 사이 마크다운 잔재(--- 등) 제거 → compile 가능하게
    trace = _run_tracer(code)
    if not trace:
        return None
    src_lines = code.split("\n")
    label_lines = _labelable_linenos(code)
    first_order = _first_exec_order(trace, label_lines)
    if len(first_order) < _EXEC_PICK:
        return None
    chosen_exec = first_order[:_EXEC_PICK]        # 처음 실행되는 앞 N줄(= 실행 순서)
    by_source = sorted(chosen_exec)               # 소스(줄번호) 순서 = 라벨 ①②③④
    label = {ln: i for i, ln in enumerate(by_source)}
    answer = tuple(label[ln] for ln in chosen_exec)   # 실행 순서를 라벨로
    if answer == tuple(range(len(by_source))):    # 줄번호 순 == 실행 순 → 너무 쉬움 → 폴백
        return None
    distractors = _perm_distractors(answer)
    if len(distractors) < 3:
        return None
    cands = [answer] + distractors[:3]

    def fmt(t):
        return " → ".join(_CIRCLED[i] for i in t)

    order = list(range(4))
    random.shuffle(order)
    shuffled = [cands[i] for i in order]
    options = [f"{chr(65 + i)}. {fmt(t)}" for i, t in enumerate(shuffled)]
    answer_label = chr(65 + order.index(0))

    labeled = "\n".join(f"{_CIRCLED[i]} {src_lines[ln - 1].strip()}"
                        for i, ln in enumerate(by_source))
    stem = ("다음 코드에서 아래 ①~④ 줄이 '처음 실행되는' 순서로 옳은 것은?\n" + labeled)
    return {
        "ct_skill":     "알고리즘적사고",
        "question":     stem,
        "options":      options,
        "answer":       answer_label,
        "answer_type":  "conceptual",       # _verify_one(스니펫 실행)이 건드리지 않게
        "type":         "execution_order",  # quiz_spec §5 추가 필드
        "track":        "code",
        "verify_method": "trace",
        "verified":     True,
        "explanation":  "함수는 정의된 줄이 아니라 '호출될 때' 안쪽 줄이 실행돼요. "
                        "줄 번호 순서가 아니라 실제 호출 흐름을 따라간 순서가 정답이에요.",
        "focus_points": ["함수 정의보다 그 함수를 '부르는 줄'이 먼저 실행돼요",
                         "위에서 아래가 아니라 실제 호출 흐름을 따라가 보기"],
        "_exec_lines":  by_source,           # 라벨 순서(소스순) — 검증 재현용
    }


def _validate_execution_order(code: str, q: dict) -> bool:
    """검증 게이트: 트레이스 재현(결정성) + 보기 4개 상이 + 정답이 실행 순서와 일치."""
    if not q or q.get("type") != "execution_order":
        return False
    opts = q.get("options", [])
    if len(opts) != 4 or len({o.split(". ", 1)[-1] for o in opts}) != 4:
        return False                 # 보기 4개가 모두 서로 달라야(정답 유일)
    by_source = q.get("_exec_lines")
    if not by_source:
        return False
    code = _clean_code(code)         # build와 동일하게 정제(줄 번호 일관)
    trace = _run_tracer(code)        # 트레이스 재현(결정성)
    if not trace:
        return False
    label = {ln: i for i, ln in enumerate(by_source)}
    chosen = set(by_source)
    seen, exec_order = set(), []     # 선택된 줄들의 첫 실행 순서 재계산
    for ln in trace:
        if ln in chosen and ln not in seen:
            seen.add(ln)
            exec_order.append(label[ln])
    if len(exec_order) != len(by_source):
        return False
    expected = " → ".join(_CIRCLED[i] for i in exec_order)
    ans_opt = next((o for o in opts if o.startswith(q.get("answer", "") + ".")), "")
    return ans_opt.split(". ", 1)[-1] == expected


def _apply_execution_order(questions: list, code: str) -> list:
    """알고리즘적사고 슬롯을 '실행 순서 고르기'로 교체. 실패 시 기존 코드 문항 유지."""
    out = []
    for q in questions:
        if q.get("ct_skill") == "알고리즘적사고":
            eq = _build_execution_order(code)
            if eq and _validate_execution_order(code, eq):
                out.append(eq)
                continue
            print("[execution_order] 생성/검증 실패 → 기존 코드 문항 유지")
        out.append(q)
    return out


# ══════════════════════════════════════════════════════════════════
# 비코드 트랙 — 패턴인식(규칙 검증) · 분해(intent-first) (quiz_spec §4.B)
# 패턴: 코드가 규칙(공차·공비·주기)으로 수열·정답·오답을 결정 → 자동 검증(LLM 미사용).
# 분해: LLM이 intent-first로 저작 → 구조 검증 + 사람 검수(verified=False, 자동검증 불가).
# 코드 트랙·_verify_one은 불변. 분해/패턴인식 슬롯만 교체(실패 시 코드형 폴백).
# ══════════════════════════════════════════════════════════════════

_PATTERN_SHOW = 4   # 보여줄 항 수(그다음 항이 정답)
_PATTERN_DECOYS = ["◆", "★", "○", "□", "♠", "♥"]


def _pattern_term(rule: dict, n: int):
    """규칙으로 n번째(0-index) 항을 계산한다(자동 검증의 단일 출처)."""
    t = rule["type"]
    if t == "arithmetic":
        return rule["start"] + rule["step"] * n
    if t == "geometric":
        return rule["start"] * (rule["ratio"] ** n)
    if t == "cycle":
        seq = rule["seq"]
        return seq[n % len(seq)]
    raise ValueError(f"알 수 없는 규칙: {t}")


# 코드 소재(단위)로 패턴 문항의 맥락을 잡는다 → 코드와 동떨어지지 않게(사용자 피드백).
_PATTERN_THEMES = [
    ("원", "어떤 물건의 가격이 다음과 같이 바뀌어요.", "money"),
    ("점", "어떤 학생의 점수가 다음과 같이 바뀌어요.", "small"),
    ("명", "어떤 모임에 모인 사람 수가 다음과 같이 바뀌어요.", "small"),
    ("개", "어떤 물건의 개수가 다음과 같이 바뀌어요.", "small"),
    ("권", "쌓인 책의 권수가 다음과 같이 바뀌어요.", "small"),
    ("회", "어떤 일을 한 횟수가 다음과 같이 바뀌어요.", "small"),
]


def _infer_pattern_theme(code: str):
    """코드에 등장하는 단위로 패턴 맥락(단위·도입문·수 규모)을 고른다. 없으면 추상 수열."""
    for unit, intro, scale in _PATTERN_THEMES:
        if unit in (code or ""):
            return unit, intro, scale
    return "", "", "small"


def _gen_pattern_rule(scale: str, unit: str) -> dict:
    kind = random.choice(["arithmetic", "geometric", "cycle"])
    if unit and kind == "cycle":            # 실생활 단위가 있으면 숫자 규칙(주기 도형 제외)
        kind = random.choice(["arithmetic", "geometric"])
    if kind == "cycle":
        base = random.choice([["▲", "●", "■"], ["●", "■", "▲", "◆"], ["1", "2", "3"], ["가", "나", "다"]])
        return {"type": "cycle", "seq": base, "label": "주기적으로 반복되는", "unit": ""}
    if scale == "money":                    # 가격: 둥근 수(1000·2000·4000·8000 …)
        if kind == "arithmetic":
            return {"type": "arithmetic", "start": random.choice([500, 1000, 2000, 3000]),
                    "step": random.choice([500, 1000, 2000]), "label": "일정한 값을 더하는", "unit": unit}
        return {"type": "geometric", "start": random.choice([500, 1000]), "ratio": 2,
                "label": "일정한 값을 곱하는(두 배씩 늘어나는)", "unit": unit}
    if kind == "arithmetic":
        return {"type": "arithmetic", "start": random.randint(1, 9),
                "step": random.choice([2, 3, 4, 5]), "label": "일정한 수를 더하는", "unit": unit}
    return {"type": "geometric", "start": random.choice([1, 2, 3]),
            "ratio": random.choice([2, 3]), "label": "일정한 수를 곱하는", "unit": unit}


def _pattern_cell_str(rule: dict, n: int) -> str:
    """n번째 항을 단위까지 붙여 표시 문자열로(검증·표시 단일 출처)."""
    v = _pattern_term(rule, n)
    if rule["type"] == "cycle":
        return str(v)
    return f"{v}{rule.get('unit', '')}"


def _pattern_distractors(rule: dict, shown: list, answer):
    """규칙별 흔한 오개념 오답 3개(정답과 상이·상호 유일). ±1 같은 어색한 값은 안 쓴다."""
    seen, out = {answer}, []
    if rule["type"] == "cycle":
        pool = [s for s in dict.fromkeys(rule["seq"]) if s != answer]
        pool += [d for d in _PATTERN_DECOYS if d not in rule["seq"]]
    elif rule["type"] == "arithmetic":
        d = rule["step"]
        pool = [shown[-1], answer + d, answer + 2 * d, shown[-1] * 2]   # 직전항·과다·등비 착각
    else:  # geometric
        r = rule["ratio"]
        diff = shown[-1] - shown[-2]
        pool = [shown[-1], answer * r, shown[-1] + diff, answer + shown[-1]]  # 직전·과다·등차 착각·합
    for v in pool:
        ok = (v != answer and v not in seen)
        if rule["type"] != "cycle":
            ok = ok and isinstance(v, int) and v >= 0
        if ok:
            seen.add(v)
            out.append(v)
        if len(out) == 3:
            break
    return out


def _build_pattern_question(code: str = ""):
    """비코드 패턴인식 문항 1개. 코드 소재(단위)로 맥락을 잡고, 규칙으로 수열·정답·오답을 결정."""
    unit, intro, scale = _infer_pattern_theme(code)
    rule = _gen_pattern_rule(scale, unit)
    show_n = (max(_PATTERN_SHOW, len(rule["seq"]) + 1) if rule["type"] == "cycle"
              else _PATTERN_SHOW)
    shown = [_pattern_term(rule, i) for i in range(show_n)]
    answer = _pattern_term(rule, show_n)
    distractors = _pattern_distractors(rule, shown, answer)
    if len(distractors) < 3:
        return None

    def fmt(v):
        return str(v) if rule["type"] == "cycle" else f"{v}{rule.get('unit', '')}"

    sep = " " if rule["type"] == "cycle" else ", "
    disp = sep.join(_pattern_cell_str(rule, i) for i in range(show_n)) + sep + "?"
    ans_str = _pattern_cell_str(rule, show_n)
    cands = [ans_str] + [fmt(d) for d in distractors]
    if len(set(cands)) != 4:
        return None
    order = list(range(4))
    random.shuffle(order)
    shuffled = [cands[i] for i in order]
    options = [f"{chr(65 + i)}. {s}" for i, s in enumerate(shuffled)]
    answer_label = chr(65 + order.index(0))
    intro_line = (intro if (unit and rule["type"] != "cycle")
                  else f"다음은 {rule['label']} 규칙으로 이어지는 나열이에요.")
    stem = f"{intro_line} ?에 들어갈 것으로 알맞은 것은?\n{disp}"
    return {
        "ct_skill":     "패턴인식",
        "question":     stem,
        "options":      options,
        "answer":       answer_label,
        "answer_type":  "conceptual",
        "type":         "pattern",
        "track":        "noncode",
        "verify_method": "rule",
        "verified":     True,
        "explanation":  f"앞의 항들을 보면 {rule['label']} 규칙이에요. "
                        "그 규칙을 다음에 그대로 적용한 것이 정답이에요.",
        "focus_points": ["앞의 항끼리 어떻게 변하는지(더하기·곱하기·반복) 살펴보기",
                         "찾은 규칙을 다음 항에 그대로 적용하기"],
        "_pattern_rule":   rule,
        "_pattern_answer": ans_str,
    }


def _verify_pattern_question(q: dict) -> bool:
    """규칙으로 정답 항을 재계산해 보기와 일치하는지 자동 검증(quiz_spec §4.B)."""
    if not q or q.get("type") != "pattern":
        return False
    opts = q.get("options", [])
    if len(opts) != 4 or len({o.split(". ", 1)[-1] for o in opts}) != 4:
        return False
    rule = q.get("_pattern_rule")
    if not rule:
        return False
    show_n = (max(_PATTERN_SHOW, len(rule["seq"]) + 1) if rule["type"] == "cycle"
              else _PATTERN_SHOW)
    recomputed = _pattern_cell_str(rule, show_n)
    ans_opt = next((o for o in opts if o.startswith(q.get("answer", "") + ".")), "")
    return ans_opt.split(". ", 1)[-1] == recomputed and recomputed == q.get("_pattern_answer")


def _decomp_distractors(steps: list) -> list:
    """정답 단계 순서에서 흔한 오개념 오답(순서 바꾸기·단계 합치기[누락]·역순)을 만든다.
    정답(steps 원순서)과 다르고 상호 유일. quiz_spec §4.B."""
    seen, out = {tuple(steps)}, []

    def add(seq):
        t = tuple(seq)
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            out.append(list(seq))

    for i in range(len(steps) - 1):              # 인접 두 단계 순서 바꾸기
        m = steps[:]
        m[i], m[i + 1] = m[i + 1], m[i]
        add(m)
    for i in range(len(steps)):                  # 한 단계 빼기(두 단계 합치기 → 단계 수 -1)
        add(steps[:i] + steps[i + 1:])
    if len(steps) >= 3:                          # 역순
        add(steps[::-1])
    return out


def _build_decomposition_question(code: str = "", templates: str = ""):
    """비코드 분해 문항 1개. LLM은 intent-first로 '정답 단계(steps)'만 저작하고,
    정답 보기·오답은 코드가 steps로 구성한다(정답이 구조적으로 확정 → MCQ 내적 일관).
    code: 소재 참고용 — 코드가 다루는 일과 관련된 상황으로 만들되 코드는 노출하지 않는다.
    단, steps가 상황을 올바로 분해했는지는 자동 검증 불가 → verified=False(사람 검수).
    실패 시 None(→ 코드형 폴백)."""
    for attempt in range(VERIFY_RETRY_LIMIT):
        try:
            raw = llm.call_decomposition_gen(code, templates)
            d = json.loads(_slice_first_json(raw), strict=False)
        except Exception as e:
            print(f"[decomposition] {attempt + 1}/{VERIFY_RETRY_LIMIT} 파싱 실패 — {e}")
            continue
        situation = (d.get("situation") or "").strip()
        steps = [s.strip() for s in (d.get("steps") or []) if isinstance(s, str) and s.strip()]
        if not situation or not (3 <= len(steps) <= 5) or len(set(steps)) != len(steps):
            continue
        distractors = _decomp_distractors(steps)
        cands = [steps] + distractors[:3]
        opt_texts = [" → ".join(x) for x in cands]
        if len(set(opt_texts)) != 4:             # 보기 4개가 모두 서로 달라야(정답 유일)
            continue
        order = list(range(4))
        random.shuffle(order)
        shuffled = [opt_texts[i] for i in order]
        options = [f"{chr(65 + i)}. {s}" for i, s in enumerate(shuffled)]
        answer = chr(65 + order.index(0))        # 정답 = 원순서 steps (구조적으로 확정)
        return {
            "ct_skill":     "분해",
            "question":     f"{situation}\n\n이 일을 순서대로 단계로 나눈 것으로 알맞은 것은?",
            "options":      options,
            "answer":       answer,
            "answer_type":  "conceptual",
            "type":         "decomposition",
            "track":        "noncode",
            "verify_method": "authored",   # 단계 자체의 타당성은 사람 검수(자동 검증 불가)
            "verified":     False,         # quiz_spec §4.B·§9 — 사람 검수 권장
            "explanation":  (d.get("explanation") or "").strip(),
            "focus_points": ["작업을 시간·논리 순서의 단계로 나눠 보기",
                             "단계의 순서가 바뀌거나 빠진 보기를 가려내기"],
            "_decomp_steps": steps,
        }
    print(f"[decomposition] {VERIFY_RETRY_LIMIT}회 실패 → 기존 코드 문항 유지")
    return None


def _apply_noncode_questions(questions: list, code: str = "") -> list:
    """분해·패턴인식 슬롯을 비코드 문항으로 교체. 실패 시 기존 코드 문항 유지(회귀 0).
    code: 코드 소재와 연결된 문항이 되도록 빌더에 전달(패턴=단위 맥락, 분해=관련 상황).
    패턴인식=규칙 자동검증, 분해=구조검증+사람검수(verified=False)."""
    out = []
    for q in questions:
        sk = q.get("ct_skill")
        nq = None
        if sk == "패턴인식":
            nq = _build_pattern_question(code)
            if nq and not _verify_pattern_question(nq):
                nq = None
        elif sk == "분해":
            nq = _build_decomposition_question(code)
        if nq:
            out.append(nq)
            continue
        if sk in ("패턴인식", "분해"):
            print(f"[noncode {sk}] 생성/검증 실패 → 기존 코드 문항 유지")
        out.append(q)
    return out


def _student_safe_questions(questions: list) -> list:
    """학생 응답용 문항 필드만 추린다. 내부 전용 필드(focus_points 등)는 제외."""
    return [
        {
            "ct_skill":    q.get("ct_skill"),
            "question":    q.get("question"),
            "options":     q.get("options"),
            "answer":      q.get("answer"),
            "explanation": q.get("explanation", ""),
            # quiz_spec §5: 유형 메타. 코드 자체 문항은 None(기존 호환), 신규 유형만 값을 가진다.
            "type":        q.get("type"),
        }
        for q in questions
    ]


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
_CJK_RE = re.compile(r"[一-鿿]")   # 한자(중국어) 혼입 탐지


def _clean_code(code: str) -> str:
    """코드 사이에 낀 마크다운 구분선 등 비파이썬 잔재를 제거한다."""
    return "\n".join(ln for ln in code.split("\n") if not _MD_LINE_RE.match(ln)).strip()


def _gen_code_within_limit(topic: str, ctx: str, difficulty: str) -> str:
    """코드를 생성하되 (1) CODE_MAX_LINES 초과 또는 (2) 한자(중국어) 혼입이면 재생성한다
    (최대 CODE_GEN_RETRY회). 9B 모델이 20줄 제한을 어기거나 문자열에 한자를 섞는 문제 대응.
    전부 부적합하면 가장 나은 후보(한자 없음 우선 → 짧은 것)를 쓴다."""
    best = None
    for attempt in range(CODE_GEN_RETRY):
        code = _clean_code(strip_fences(llm.call_code_gen(topic=topic, ctx=ctx, difficulty=difficulty)))
        n = _code_line_count(code)
        has_cjk = bool(_CJK_RE.search(code))
        if n <= CODE_MAX_LINES and not has_cjk:
            return code
        score = n + (1000 if has_cjk else 0)   # 한자 포함은 큰 패널티로 후순위
        if best is None or score < best[1]:
            best = (code, score)
        why = []
        if n > CODE_MAX_LINES: why.append(f"{n}줄>{CODE_MAX_LINES}")
        if has_cjk: why.append("한자혼입")
        print(f"[generate-code] {attempt+1}/{CODE_GEN_RETRY} 재생성 — {', '.join(why)}")
    print("[generate-code] 제한 내 실패 → 가장 나은 후보 사용")
    return best[0]


@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)
    ctx = rag_store.retrieve("code_examples", topic)
    try:
        code = _gen_code_within_limit(topic, ctx, TARGET_DIFFICULTY)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"code": code, "topic": topic})


def _retrieve_templates(code: str) -> str:
    """CT 5유형별 출제 가이드를 검색해 합친다.
    quiz_spec §7 — type 필터: 각 유형은 자기 코퍼스(ct_{유형}.txt)에서만 retrieve해
    코드형/비코드 코퍼스가 유형 간에 섞이지 않게 한다(검색 조건화, 인덱싱 불변)."""
    parts = []
    for skill in PROBLEM_CT_SKILLS:
        chunk = rag_store.retrieve(
            "problem_templates", f"{skill} {code[:200]}", k=2,
            filter={"source_file": f"ct_{skill}.txt"},
        )
        if chunk:
            parts.append(f"[{skill}]\n{chunk}")
    return "\n\n".join(parts)


@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    """한 번 호출로 MCQ 5문항 세트를 생성한다 (분해→통합 순)."""
    data = request.get_json() or {}
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "code 필드 필요"}), 400
    difficulty = TARGET_DIFFICULTY   # 난이도 통일 (클라이언트 입력 무시)
    session_id = (data.get("session_id") or "").strip()

    # 5유형별로 RAG 가이드를 type 필터로 따로 검색해 합친다 (유형별 코퍼스 분리)
    templates = _retrieve_templates(code)

    # 세트 단위 파싱 재시도 (구조 깨졌을 때만)
    parsed = None
    last_error = None
    for attempt in range(PROBLEM_RETRY_LIMIT):
        try:
            raw = llm.call_problem_gen(code, templates, difficulty)
            parsed = _parse_problem_set(raw)
            break
        except Exception as e:
            last_error = e
            print(f"[generate-problem set] {attempt+1}/{PROBLEM_RETRY_LIMIT} 파싱 실패: {e}")
    if parsed is None:
        return jsonify({"error": str(last_error)}), 503

    # CT 유형 구성 보정(중복/누락 보충)
    reconciled = _reconcile_skills(parsed["questions"], code, templates)
    # 알고리즘적사고 슬롯을 '실행 순서 고르기'(트레이스 검증)로 교체(실패 시 코드형 유지).
    # process_questions보다 먼저 — 교체된 conceptual 문항이 스킵되지 않고 통과하도록.
    reconciled = _apply_execution_order(reconciled, code)
    # 분해·패턴인식 슬롯을 코드 소재와 연결된 비코드 문항으로 교체(패턴=단위 맥락, 분해=관련 상황). 실패 시 코드형.
    reconciled = _apply_noncode_questions(reconciled, code)
    # 문항별 검증 + 실패시 단일 문항 재생성/스킵 (비코드·실행순서는 conceptual이라 그대로 통과)
    final_questions = _process_questions(reconciled, code, templates)
    _save_problem_set(session_id, final_questions)   # 내부 보관(focus_points 포함)
    return jsonify({
        "title":     parsed["title"],
        "summary":   parsed["summary"],
        "questions": _student_safe_questions(final_questions),
    })


def _gen_session_id() -> str:
    return f"{int(datetime.now().timestamp())}_{random.randint(1000, 9999)}"


@api.route("/session/start", methods=["POST"])
def session_start():
    """
    한 번의 호출로 코드 1개 + MCQ 5문항 세트를 생성·반환한다.
    응답 스키마는 code_reading_generation.md §4 (학생 전송용 — 내부 전용 필드 제외).
    """
    data = request.get_json(silent=True) or {}
    difficulty = TARGET_DIFFICULTY   # 난이도 통일 (클라이언트 입력 무시)
    topic_hint = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)

    # Stage 1 — 단일 완결 코드 생성
    code_ctx = rag_store.retrieve("code_examples", topic_hint)
    try:
        raw_code = llm.call_code_gen(topic=topic_hint, ctx=code_ctx, difficulty=difficulty)
        code = strip_fences(raw_code)
    except Exception as e:
        return jsonify({"error": f"코드 생성 실패: {e}"}), 503

    # Stage 2 — 5유형별 RAG 가이드(type 필터)를 합치고 MCQ 5문항 일괄 생성
    templates = _retrieve_templates(code)

    # 세트 단위 파싱 재시도 (구조 깨졌을 때만)
    parsed = None
    last_error = None
    for attempt in range(PROBLEM_RETRY_LIMIT):
        try:
            raw_problems = llm.call_problem_gen(code, templates, difficulty)
            parsed = _parse_problem_set(raw_problems)
            break
        except Exception as e:
            last_error = e
            print(f"[session/start set] {attempt+1}/{PROBLEM_RETRY_LIMIT} 파싱 실패: {e}")
    if parsed is None:
        return jsonify({"error": f"문제 생성 실패: {last_error}"}), 503

    # CT 유형 구성 보정(중복/누락 보충)
    reconciled = _reconcile_skills(parsed["questions"], code, templates)
    # 알고리즘적사고 슬롯을 '실행 순서 고르기'(트레이스 검증)로 교체(실패 시 코드형 유지).
    # process_questions보다 먼저 — 교체된 conceptual 문항이 스킵되지 않고 통과하도록.
    reconciled = _apply_execution_order(reconciled, code)
    # 분해·패턴인식 슬롯을 코드 소재와 연결된 비코드 문항으로 교체(패턴=단위 맥락, 분해=관련 상황). 실패 시 코드형.
    reconciled = _apply_noncode_questions(reconciled, code)
    # 문항별 검증 + 실패시 단일 문항 재생성/스킵 (비코드·실행순서는 conceptual이라 그대로 통과)
    final_questions = _process_questions(reconciled, code, templates)
    session_id = _gen_session_id()
    _save_problem_set(session_id, final_questions)   # 내부 보관(focus_points 포함)
    return jsonify({
        "session_id": session_id,
        "title":      parsed["title"],
        "summary":    parsed["summary"],
        "difficulty": difficulty,
        "code":       code,
        "questions":  _student_safe_questions(final_questions),
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
