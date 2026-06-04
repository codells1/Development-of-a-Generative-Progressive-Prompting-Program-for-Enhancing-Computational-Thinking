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

# CT 평가 루브릭 지표 (prompts/ct_evaluation_rubric.md). 1~3점 또는 N/A로 평정.
# ⑦ 자기해결은 CT 총점과 분리 집계하므로 이 튜플에 넣지 않는다.
CT_SKILLS = ("문제분해", "용어사용", "추상화", "실행흐름", "자료표현", "동작파악", "패턴인식")
SELF_REG_SKILL = "자기해결"   # 메타인지/자기조절 — CT 총점과 분리

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


_GRADE_MAP = {"상": 3, "중": 2, "하": 1, "우수": 3, "보통": 2, "미흡": 1}


def _norm_rubric_score(value):
    """루브릭 점수 1칸을 정규화한다. 상/중/하·우수/보통/미흡 또는 1~3 정수 → 1~3,
    N/A·무효값이면 None."""
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        if s in _GRADE_MAP:
            return _GRADE_MAP[s]
        su = s.upper()
        if su in ("", "N/A", "NA", "NONE", "-"):
            return None
        try:
            value = int(float(su))
        except ValueError:
            return None
    try:
        iv = int(value)
    except (ValueError, TypeError):
        return None
    return max(1, min(3, iv))


def score_ct(ct_raw: dict) -> dict:
    """
    루브릭 원시 채점 결과(ct_raw)를 정규화·집계한다.
    - 각 CT 지표: 1~3 또는 None(N/A). N/A는 평균에서 제외.
    - 자기해결: CT 총점과 분리 집계.
    - ct_score: N/A 제외 CT 지표 평균을 0~100으로 환산(3점=100). 측정값 없으면 0.
    - weak_ct: LLM 산출값이 유효(채점된 CT 지표)하면 그대로, 아니면 최솟값 지표.
    LLM 비의존 — 순수 함수라 단독 검증 가능.
    """
    ct_scores = {k: _norm_rubric_score(ct_raw.get(k)) for k in CT_SKILLS}
    graded = {k: v for k, v in ct_scores.items() if v is not None}
    self_reg = _norm_rubric_score(ct_raw.get(SELF_REG_SKILL))

    if graded:
        avg = sum(graded.values()) / len(graded)
        ct_score = round(avg / 3 * 100)
    else:
        ct_score = 0

    weak_ct = ct_raw.get("weak_ct", "")
    if weak_ct not in graded:
        weak_ct = min(graded, key=graded.get) if graded else ""

    return {
        "ct_scores":   ct_scores,    # 값은 1~3 또는 None(=N/A)
        "self_reg":    self_reg,     # 1~3 또는 None
        "ct_score":    ct_score,     # 0~100
        "weak_ct":     weak_ct,
        "feedback":    (ct_raw.get("feedback") or "").strip(),
    }


def save_feedback(session_id: str, topic: str, scored: dict) -> None:
    """정규화된 CT 채점 결과(score_ct 반환값)를 feedback.json에 누적 저장한다."""
    try:
        records = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # JSON 저장 시 None은 "N/A" 문자열로 직렬화해 가독성 유지
        ct_scores = {k: (v if v is not None else "N/A")
                     for k, v in scored.get("ct_scores", {}).items()}
        self_reg = scored.get("self_reg")
        records.append({
            "session_id":  session_id,
            "timestamp":   now,
            "topic":       topic,
            "ct_scores":   ct_scores,
            "self_reg":    self_reg if self_reg is not None else "N/A",
            "ct_score":    scored.get("ct_score", 0),
            "weak_ct":     scored.get("weak_ct", ""),
            "ct_feedback": scored.get("feedback", ""),
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


def _run_verification_snippet(snippet: str, code: str = "") -> str:
    """
    verification_snippet을 임시 .py 파일로 저장해 별도 subprocess로 실행한다.
    code가 주어지면 원본 프로그램의 정의를 먼저 주입한다(_build_verification_program).
    타임아웃 VERIFY_TIMEOUT_SEC초, stdout.strip() 반환. 실패/타임아웃은 raise.
    (subprocess.run이 타임아웃 시 자식을 종료한 뒤 TimeoutExpired를 올린다.)
    """
    program = _build_verification_program(snippet, code)
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
    conceptual·빈 스니펫은 ok=True. ★ 정답을 실행값으로 덮어쓰지 않는다.
    code: 원본 프로그램. 스니펫이 그 함수·전역을 참조할 수 있도록 주입한다.
    """
    if q.get("answer_type") != "computational":
        return (True, "skip (conceptual)")
    snippet = (q.get("verification_snippet") or "").strip()
    if not snippet:
        return (True, "skip (empty)")
    ans_label = q.get("answer", "")
    ans_option = next((o for o in q.get("options", []) if o.strip().startswith(ans_label)), "")
    expected = _option_value(ans_option, ans_label)
    try:
        actual = _run_verification_snippet(snippet, code)
    except subprocess.TimeoutExpired:
        return (False, f"타임아웃 ({VERIFY_TIMEOUT_SEC}s)")
    except Exception as e:
        return (False, f"실행 오류 — {e}")
    if not _values_match(actual, expected):
        return (False, f"불일치 stdout={actual!r}, 정답 보기={expected!r}")
    return (True, f"일치 {actual!r}")


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


def _student_safe_questions(questions: list) -> list:
    """학생 응답용 문항 필드만 추린다. 내부 전용 필드(focus_points 등)는 제외."""
    return [
        {
            "ct_skill":    q.get("ct_skill"),
            "question":    q.get("question"),
            "options":     q.get("options"),
            "answer":      q.get("answer"),
            "explanation": q.get("explanation", ""),
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


@api.route("/status", methods=["GET"])
def status():
    try:
        models = llm._client.models.list()
        return jsonify({"connected": True, "models": [m.id for m in models.data]})
    except Exception:
        return jsonify({"connected": False, "models": []})


@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)
    ctx = rag_store.retrieve("code_examples", topic)
    try:
        raw = llm.call_code_gen(topic=topic, ctx=ctx)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

    return jsonify({"code": strip_fences(raw), "topic": topic})


@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    """한 번 호출로 MCQ 5문항 세트를 생성한다 (분해→통합 순)."""
    data = request.get_json() or {}
    code = data.get("code", "")
    if not code:
        return jsonify({"error": "code 필드 필요"}), 400
    difficulty = (data.get("difficulty") or "").strip()
    session_id = (data.get("session_id") or "").strip()

    # 5유형별로 RAG 가이드를 따로 검색해 합친다 (한 쿼리에 5유형 섞으면 편향됨)
    template_parts = []
    for skill in PROBLEM_CT_SKILLS:
        chunk = rag_store.retrieve("problem_templates", f"{skill} {code[:200]}", k=2)
        if chunk:
            template_parts.append(f"[{skill}]\n{chunk}")
    templates = "\n\n".join(template_parts)

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

    # CT 유형 구성 보정(중복/누락 보충) → 문항별 검증 + 실패시 단일 문항 재생성/스킵
    reconciled = _reconcile_skills(parsed["questions"], code, templates)
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
    difficulty = (data.get("difficulty") or "").strip()
    topic_hint = (data.get("topic_hint") or "").strip() or random.choice(CODE_TOPIC_HINTS)

    # Stage 1 — 단일 완결 코드 생성
    code_ctx = rag_store.retrieve("code_examples", topic_hint)
    try:
        raw_code = llm.call_code_gen(topic=topic_hint, ctx=code_ctx, difficulty=difficulty)
        code = strip_fences(raw_code)
    except Exception as e:
        return jsonify({"error": f"코드 생성 실패: {e}"}), 503

    # Stage 2 — 5유형별 RAG 가이드 합치고 MCQ 5문항 일괄 생성
    template_parts = []
    for skill in PROBLEM_CT_SKILLS:
        chunk = rag_store.retrieve("problem_templates", f"{skill} {code[:200]}", k=2)
        if chunk:
            template_parts.append(f"[{skill}]\n{chunk}")
    templates = "\n\n".join(template_parts)

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

    # CT 유형 구성 보정(중복/누락 보충) → 문항별 검증 + 실패시 단일 문항 재생성/스킵
    reconciled = _reconcile_skills(parsed["questions"], code, templates)
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
        # <think>...</think> 추론 토큰을 학생 화면에 내보내지 않도록 스트림에서 제거
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

        # 루브릭(prompts/ct_evaluation_rubric.md) 1~3·N/A 채점 → 정규화·집계
        scored = score_ct(ct_raw)
        save_feedback(session_id, topic, scored)

        # 프론트는 ct_score(0~100)와 ct_feedback만 사용. 지표 상세는 N/A→"N/A"로 직렬화.
        ct_scores_out = {k: (v if v is not None else "N/A")
                         for k, v in scored["ct_scores"].items()}
        return jsonify({
            "ct_scores":   ct_scores_out,
            "self_reg":    scored["self_reg"] if scored["self_reg"] is not None else "N/A",
            "weak_ct":     scored["weak_ct"],
            "ct_score":    scored["ct_score"],
            "ct_feedback": scored["feedback"],
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
