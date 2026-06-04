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

CT_SKILLS = ("분해", "패턴인식", "추상화", "알고리즘적사고")

# 문제 출제용 5유형 (분석용 CT_SKILLS 4지표와 별개). 순서 고정 — 분해→통합.
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


def save_feedback(session_id: str, topic: str, ct: dict) -> None:
    """CT 점수를 feedback.json에 누적 저장한다."""
    try:
        records = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records.append({
            "session_id":  session_id,
            "timestamp":   now,
            "topic":       topic,
            "ct_scores":   {k: ct.get(k, 1) for k in CT_SKILLS},
            "weak_ct":     ct.get("weak_ct", ""),
            "ct_feedback": ct.get("feedback", ""),
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


def _parse_problem_set(raw: str) -> dict:
    """문제 세트 JSON 파싱 + 구조 검증. {title, summary, questions} 반환. 실패 시 ValueError."""
    data = json.loads(_slice_first_json(raw), strict=False)
    questions = data.get("questions")
    if not isinstance(questions, list) or len(questions) != 5:
        n = len(questions) if isinstance(questions, list) else "N/A"
        raise ValueError(f"questions 5개 필요 (현재 {n})")

    for i, q in enumerate(questions):
        if not q.get("question"):
            raise ValueError(f"q{i}: question 필드 없음")
        opts = q.get("options", [])
        if not (isinstance(opts, list) and len(opts) == 4):
            n = len(opts) if isinstance(opts, list) else "N/A"
            raise ValueError(f"q{i}: options 4개 필요 (현재 {n}개)")
        if q.get("answer") not in ("A", "B", "C", "D"):
            raise ValueError(f"q{i}: answer 라벨 오류: {q.get('answer')!r}")
        atype = q.get("answer_type")
        if atype not in ("computational", "conceptual"):
            raise ValueError(f"q{i}: answer_type 오류: {atype!r}")
        vs = q.get("verification_snippet")
        if not isinstance(vs, str):
            raise ValueError(f"q{i}: verification_snippet 필드 없음/타입 오류")
        if atype == "computational" and not vs.strip():
            raise ValueError(f"q{i}: computational 문항은 verification_snippet 필수")
        if atype == "conceptual" and vs.strip():
            raise ValueError(f"q{i}: conceptual 문항은 verification_snippet 빈 문자열이어야 함")
        fp = q.get("focus_points")
        if not isinstance(fp, list) or not (1 <= len(fp) <= 3):
            n = len(fp) if isinstance(fp, list) else "N/A"
            raise ValueError(f"q{i}: focus_points 1~3개 배열 필요 (현재 {n})")
        if not all(isinstance(x, str) and x.strip() for x in fp):
            raise ValueError(f"q{i}: focus_points 각 항목은 비어있지 않은 문자열이어야 함")

    skills = tuple(q.get("ct_skill") for q in questions)
    if skills != PROBLEM_CT_SKILLS:
        raise ValueError(f"ct_skill 순서 오류: {skills} (기대: {PROBLEM_CT_SKILLS})")

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


def _run_verification_snippet(snippet: str) -> str:
    """
    verification_snippet을 임시 .py 파일로 저장해 별도 subprocess로 실행한다.
    타임아웃 VERIFY_TIMEOUT_SEC초, stdout.strip() 반환. 실패/타임아웃은 raise.
    (subprocess.run이 타임아웃 시 자식을 종료한 뒤 TimeoutExpired를 올린다.)
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(snippet)
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


def _verify_one(q: dict) -> tuple:
    """
    단일 computational 문항 실행 검증. (ok: bool, detail: str) 반환.
    conceptual·빈 스니펫은 ok=True. ★ 정답을 실행값으로 덮어쓰지 않는다.
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
        actual = _run_verification_snippet(snippet)
    except subprocess.TimeoutExpired:
        return (False, f"타임아웃 ({VERIFY_TIMEOUT_SEC}s)")
    except Exception as e:
        return (False, f"실행 오류 — {e}")
    if actual != expected:
        return (False, f"불일치 stdout={actual!r}, 정답 보기={expected!r}")
    return (True, f"일치 {actual!r}")


def _parse_single_problem(raw: str, expected_ct_skill: str = None) -> dict:
    """단일 문항 JSON 파싱 + 기본 검증. 재생성 응답용."""
    q = json.loads(_slice_first_json(raw), strict=False)
    if not q.get("question"):
        raise ValueError("question 필드 없음")
    opts = q.get("options", [])
    if not (isinstance(opts, list) and len(opts) == 4):
        raise ValueError("options 4개 필요")
    if q.get("answer") not in ("A", "B", "C", "D"):
        raise ValueError(f"answer 라벨 오류: {q.get('answer')!r}")
    atype = q.get("answer_type")
    if atype not in ("computational", "conceptual"):
        raise ValueError(f"answer_type 오류: {atype!r}")
    vs = q.get("verification_snippet")
    if not isinstance(vs, str):
        raise ValueError("verification_snippet 필드 누락")
    if atype == "computational" and not vs.strip():
        raise ValueError("computational 문항은 verification_snippet 필수")
    if atype == "conceptual" and vs.strip():
        raise ValueError("conceptual 문항은 verification_snippet 빈 문자열이어야 함")
    fp = q.get("focus_points")
    if not isinstance(fp, list) or not (1 <= len(fp) <= 3):
        n = len(fp) if isinstance(fp, list) else "N/A"
        raise ValueError(f"focus_points 1~3개 배열 필요 (현재 {n})")
    if not all(isinstance(x, str) and x.strip() for x in fp):
        raise ValueError("focus_points 각 항목은 비어있지 않은 문자열이어야 함")
    if expected_ct_skill:
        q["ct_skill"] = expected_ct_skill   # 시스템이 요구한 CT 요소로 강제 정렬
    return q


def _process_questions(questions: list, code: str, templates: str) -> list:
    """
    spec §3.4 — 검증 실패한 computational 문항만 최대 VERIFY_RETRY_LIMIT회 재생성.
    재생성도 모두 실패하면 그 문항을 결과에서 제외(skip, 정답 보기 덮어쓰지 않음).
    conceptual은 그대로 통과.
    """
    out = []
    for i, q in enumerate(questions):
        ok, detail = _verify_one(q)
        if ok:
            out.append(q)
            continue

        ct_skill = q.get("ct_skill", "")
        print(f"[verify q{i} {ct_skill}] 초기 실패 — {detail}")

        replaced = None
        for attempt in range(VERIFY_RETRY_LIMIT):
            try:
                raw = llm.call_single_problem_gen(code, ct_skill, templates)
                new_q = _parse_single_problem(raw, expected_ct_skill=ct_skill)
            except Exception as e:
                print(f"[regen q{i} {ct_skill}] {attempt+1}/{VERIFY_RETRY_LIMIT} 파싱 실패 — {e}")
                continue
            ok2, detail2 = _verify_one(new_q)
            if ok2:
                print(f"[regen q{i} {ct_skill}] {attempt+1}/{VERIFY_RETRY_LIMIT} 성공 — {detail2}")
                replaced = new_q
                break
            print(f"[regen q{i} {ct_skill}] {attempt+1}/{VERIFY_RETRY_LIMIT} 검증 실패 — {detail2}")

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

    # 문항별 검증 + 실패시 단일 문항 재생성/스킵
    final_questions = _process_questions(parsed["questions"], code, templates)
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

    # 문항별 검증 + 실패시 단일 문항 재생성/스킵
    final_questions = _process_questions(parsed["questions"], code, templates)
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
        messages = [{"role": "system", "content": system_prompt}] + history
        user_message_for_log = history[-1]["content"] if history else ""

    full_reply_holder = []

    def generate():
        try:
            stream = llm.stream_chatbot(messages)
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_reply_holder.append(delta)
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        full_reply = "".join(full_reply_holder)
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

    # 대화 로그 직렬화 — 분석 대상 자료, messages 맥락 아님
    qa_pairs = ""
    for i, (p, a) in enumerate(zip(problems, answers), 1):
        qa_pairs += f"\n{i}. [문제] {p}\n   [학생선택] {a}\n"
    chat_text = ""
    for msg in chat_history:
        role = "학생" if msg["role"] == "user" else "AI"
        chat_text += f"\n{role}: {msg['content']}"
    log_text = (
        f"학습 주제: {topic}\n\n"
        f"코드 예제:\n{code}\n\n"
        f"문제와 학생 답안:{qa_pairs}\n"
        f"챗봇 대화:{chat_text if chat_text else ' (없음)'}"
    )

    try:
        ct_raw = _extract_json(llm.call_log_analysis(log_text))

        # CT 4요소 점수 정규화 (1~5 클램핑)
        ct_scores = {k: max(1, min(5, int(ct_raw.get(k, 1)))) for k in CT_SKILLS}

        # weak_ct 보정: LLM 산출값이 유효 키가 아니면 최솟값 키로 재계산
        weak_ct = ct_raw.get("weak_ct", "")
        if weak_ct not in ct_scores:
            weak_ct = min(ct_scores, key=ct_scores.get)

        # 평균(1~5) → 100점 환산 (프론트 현행 호환)
        ct_score = round(sum(ct_scores.values()) / len(ct_scores) * 20)

        save_feedback(
            session_id, topic,
            {**ct_scores, "weak_ct": weak_ct, "feedback": ct_raw.get("feedback", "")},
        )

        return jsonify({
            "ct_scores":   ct_scores,
            "weak_ct":     weak_ct,
            "ct_score":    ct_score,
            "ct_feedback": ct_raw.get("feedback", ""),
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
