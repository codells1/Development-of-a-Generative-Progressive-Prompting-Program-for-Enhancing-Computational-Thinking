from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import re
import os
from datetime import datetime
import rag as rag_store
import llm

api = Blueprint("api", __name__, url_prefix="/api")

# 컨텍스트 초과 방지: LLM에 전달하는 대화 히스토리 최대 메시지 수 (user+assistant 합산)
# 줄이면 컨텍스트 안전, 늘리면 더 긴 맥락 유지. 기본 16 = 약 8턴
MAX_HISTORY_MSGS = 16

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

STAGE_MAP = {
    0: "변수·연산·조건",
    1: "반복·리스트",
    2: "함수",
    3: "알고리즘",
}

FUSION_TOPIC = "융합"

CT_SKILLS     = ("문제분해", "용어사용", "추상화", "실행흐름", "자료형태", "대안탐색", "자기해결")
PROMPT_SKILLS = ("명확성", "구체성", "맥락제공", "관련성", "자기주도성", "발전성")

FEEDBACK_FILE = os.path.join(os.path.dirname(__file__), "feedback.json")

CT_MAP = {
    0: {"ct_skill": "분해",           "difficulty": "매우쉬움"},
    1: {"ct_skill": "패턴인식",       "difficulty": "쉬움"},
    2: {"ct_skill": "추상화",         "difficulty": "보통"},
    3: {"ct_skill": "알고리즘적사고", "difficulty": "어려움"},
    4: {"ct_skill": "통합",           "difficulty": "매우어려움"},
}


def _iter_stream_content(stream):
    """스트리밍에서 Qwen3 <think>...</think> 블록을 제거하며 텍스트를 산출한다."""
    buffer = ""
    in_think = False
    OPEN_TAG, CLOSE_TAG = "<think>", "</think>"

    for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        buffer += delta

        while buffer:
            if in_think:
                idx = buffer.find(CLOSE_TAG)
                if idx == -1:
                    buffer = ""
                    break
                buffer = buffer[idx + len(CLOSE_TAG):]
                in_think = False
            else:
                idx = buffer.find(OPEN_TAG)
                if idx == -1:
                    # 버퍼 끝에 태그가 시작될 수 있는 경우 보류
                    safe = len(buffer)
                    for i in range(1, len(OPEN_TAG)):
                        if buffer.endswith(OPEN_TAG[:i]):
                            safe = len(buffer) - i
                            break
                    if safe > 0:
                        yield buffer[:safe]
                    buffer = buffer[safe:]
                    break
                else:
                    if idx > 0:
                        yield buffer[:idx]
                    buffer = buffer[idx + len(OPEN_TAG):]
                    in_think = True

    if buffer and not in_think:
        yield buffer


def strip_fences(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


def _extract_json(raw: str) -> dict:
    """응답에서 가장 바깥 { } JSON 블록을 추출·파싱한다."""
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError("JSON 블록 없음")
    return json.loads(m.group())


def _norm(s: str) -> str:
    """공백 제거 정규화 — evidence가 학생 발화에 실재하는지 대조용."""
    return re.sub(r"\s+", "", s or "")


def _clean_evidence(ev: str) -> str:
    """모델이 붙이는 역할 라벨('학생:')·따옴표를 제거해 학생 발화와 대조 가능하게 만든다."""
    ev = (ev or "").strip().strip('"\'“”‘’')
    ev = re.sub(r"^(학생|사용자|user|AI|도우미|assistant)\s*[:：\-]\s*", "", ev, flags=re.IGNORECASE)
    return ev.strip()


# 답만 요구하거나 내용이 없는 단답 — 어떤 CT 지표도 입증하지 못함(루브릭상 '답만 요구'=미관찰/1점)
_FILLER_RE       = re.compile(r"^(ㅇ+|ㄴ+|네+|음+|어+|응+|글쎄요?|몰라요?|모르겠어?요?|그냥요?)$")
_ANSWER_DEMAND_RE = re.compile(r"(답|정답).{0,4}(알려|뭐|가르|좀)|그냥.{0,3}알려|알려\s*주(세요|라|세요|십시오)?")


def _is_low_content_evidence(ev: str) -> bool:
    """근거 발화가 답 요구·무내용 단답이면 True — 그 지표는 입증되지 않은 것으로 본다."""
    t = ev.strip()
    if len(_norm(t)) < 5:
        return True
    if _FILLER_RE.match(t.replace(" ", "")):
        return True
    if _ANSWER_DEMAND_RE.search(t):
        return True
    return False


def parse_ct_evaluation(raw: str, student_text: str) -> dict:
    """7지표 CT 평가를 파싱·검증한다(채점 신뢰성은 코드가 담당).
    - 관찰 시 score를 1~3으로 클램핑, 미관찰이면 None('미흡'과 구분).
    - evidence가 학생 발화에 실재하지 않으면(=지어낸 근거) 미관찰 처리.
    - ct_total은 관찰된 score만 코드에서 합산(LLM 숫자 미신뢰).
    """
    data  = _extract_json(raw)
    items = data.get("ct_evaluation", [])
    by_name = {}
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                by_name[_norm(it.get("indicator", ""))] = it

    student_norm = _norm(student_text)
    detail = {}
    for skill in CT_SKILLS:
        it = by_name.get(_norm(skill), {})
        evidence = _clean_evidence(it.get("evidence"))
        feedback = (it.get("feedback") or "").strip()
        observed = bool(it.get("observed")) and bool(evidence)
        # 신뢰성 검증 ①: evidence(라벨·따옴표 제거 후)가 학생 발화에 실제로 존재해야 인정
        if observed and _norm(evidence) not in student_norm:
            observed, evidence = False, ""
        # 신뢰성 검증 ②: 답 요구·무내용 단답은 어떤 지표도 입증 못 함 → 미관찰
        if observed and _is_low_content_evidence(evidence):
            observed, evidence = False, ""
        if observed:
            try:
                score = max(1, min(3, int(it.get("score"))))
            except (TypeError, ValueError):
                score = 2
        else:
            score = None
        detail[skill] = {"observed": observed, "score": score,
                         "evidence": evidence, "feedback": feedback}

    observed_scores = {k: v["score"] for k, v in detail.items() if v["observed"]}
    weak_ct = min(observed_scores, key=observed_scores.get) if observed_scores else ""
    return {
        "detail":         detail,
        "ct_total":       sum(observed_scores.values()),   # 코드 합산 (0~21)
        "observed_count": len(observed_scores),
        "weak_ct":        weak_ct,
    }


def save_feedback(session_id: str, topic: str, ct: dict, prompt: dict) -> None:
    """CT 7지표 평가 + 프롬프팅 6요소 점수를 feedback.json에 누적 저장한다."""
    try:
        records = []
        if os.path.exists(FEEDBACK_FILE):
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        records.append({
            "session_id":      session_id,
            "timestamp":       now,
            "topic":           topic,
            "ct_scores":       ct.get("ct_scores", {}),       # 지표별 점수(미관찰 null)
            "ct_detail":       ct.get("ct_detail", {}),       # observed/score/evidence/feedback
            "ct_total":        ct.get("ct_total", 0),         # 코드 합산 (0~21)
            "observed_count":  ct.get("observed_count", 0),
            "weak_ct":         ct.get("weak_ct", ""),
            "ct_feedback":     ct.get("feedback", ""),
            "prompt_scores":   {k: prompt.get(k, 1) for k in PROMPT_SKILLS},
            "prompt_score":    prompt.get("overall", 0),
            "prompt_feedback": prompt.get("feedback", ""),
        })

        with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"피드백 저장 오류: {e}")


def _parse_mcq(raw: str) -> dict:
    """MCQ JSON 파싱 + 구조 검증. 실패 시 ValueError."""
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        raise ValueError("JSON 블록 없음")
    data = json.loads(m.group())
    if not data.get("question"):
        raise ValueError("question 필드 없음")
    opts = data.get("options", [])
    if not (isinstance(opts, list) and len(opts) == 4):
        raise ValueError(f"options 4개 필요 (현재 {len(opts)}개)")
    if data.get("answer") not in ("A", "B", "C", "D"):
        raise ValueError(f"answer 라벨 오류: {data.get('answer')!r}")
    return data


@api.route("/status", methods=["GET"])
def status():
    try:
        models = llm._client.models.list()
        return jsonify({"connected": True, "models": [m.id for m in models.data]})
    except Exception:
        return jsonify({"connected": False, "models": []})


@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json()
    is_fusion = data.get("is_fusion", False)
    stage = data.get("stage", 0)

    if is_fusion:
        topic = FUSION_TOPIC
        ctx = rag_store.retrieve("code_examples", topic)
        try:
            raw = llm.call_code_gen_fusion(ctx)
        except Exception as e:
            return jsonify({"error": str(e)}), 503
    else:
        topic = STAGE_MAP.get(int(stage), "변수·연산·조건")
        ctx = rag_store.retrieve("code_examples", topic)
        try:
            raw = llm.call_code_gen(topic, ctx)
        except Exception as e:
            return jsonify({"error": str(e)}), 503

    return jsonify({"code": strip_fences(raw), "topic": topic})


@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    data = request.get_json()
    code = data.get("code", "")
    problem_index = data.get("problem_index", 0)
    previous = data.get("previous_problems", [])

    ct_info   = CT_MAP[min(problem_index, 4)]
    ct_skill  = ct_info["ct_skill"]
    difficulty = ct_info["difficulty"]

    templates = rag_store.retrieve("problem_templates", f"{ct_skill} {difficulty} {code[:300]}")

    last_error = None
    for attempt in range(2):
        try:
            raw = llm.call_problem_gen(code, ct_skill, difficulty, templates, previous, problem_index)
            problem_data = _parse_mcq(raw)
            problem_data["ct_skill"]   = ct_skill
            problem_data["difficulty"] = difficulty
            return jsonify(problem_data)
        except Exception as e:
            last_error = e

    return jsonify({"error": str(last_error)}), 503


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


@api.route("/previous-feedback", methods=["GET"])
def previous_feedback():
    """feedback.json의 마지막 항목을 반환한다. 없으면 has_previous: false."""
    if not os.path.exists(FEEDBACK_FILE):
        return jsonify({"has_previous": False})
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            records = json.load(f)
        if not records:
            return jsonify({"has_previous": False})
        last = records[-1]
        return jsonify({
            "has_previous": True,
            "topic":      last.get("topic", ""),
            "ct_scores":  last.get("ct_scores", {}),
            "weak_ct":    last.get("weak_ct", ""),
            "ct_feedback": last.get("ct_feedback", ""),
        })
    except Exception:
        return jsonify({"has_previous": False})


@api.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    history = data.get("messages", [])
    session_id = data.get("session_id", "unknown")
    code_context = data.get("code_context")
    current_problem = data.get("current_problem")
    weak_ct = data.get("weak_ct")  # Task 3에서 프론트가 전달; 없으면 None → 표준 프롬프트

    # 컨텍스트 초과 방지: 최근 MAX_HISTORY_MSGS개만 유지 (시스템 메시지는 별도)
    trimmed = history[-MAX_HISTORY_MSGS:] if len(history) > MAX_HISTORY_MSGS else history
    messages = [{"role": "system", "content": llm.build_chat_system(code_context, current_problem, weak_ct)}] + trimmed
    full_reply_holder = []

    def generate():
        try:
            stream = llm.stream_chatbot(messages)
            for text in _iter_stream_content(stream):
                full_reply_holder.append(text)
                yield f"data: {json.dumps({'delta': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        full_reply = "".join(full_reply_holder)
        user_message = history[-1]["content"] if history else ""  # 원본 history에서 저장
        save_turn(session_id, user_message, full_reply, code_context)
        yield f"data: {json.dumps({'done': True})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@api.route("/chat-nudge", methods=["POST"])
def chat_nudge():
    """챗봇 선제 유도 — 오답 시 또는 무입력 시 챗봇이 먼저 소크라테스 질문을 던진다."""
    data = request.get_json()
    messages        = data.get("messages", [])
    session_id      = data.get("session_id", "unknown")
    code_context    = data.get("code_context")
    current_problem = data.get("current_problem")
    weak_ct         = data.get("weak_ct")
    reason          = data.get("reason", "inactivity")

    # 컨텍스트 초과 방지: 최근 MAX_HISTORY_MSGS개만 유지
    trimmed = messages[-MAX_HISTORY_MSGS:] if len(messages) > MAX_HISTORY_MSGS else messages
    system = llm.build_nudge_system(code_context, current_problem, reason, weak_ct)
    full_messages = [{"role": "system", "content": system}] + trimmed

    # 마지막 메시지가 assistant 또는 없을 때 Qwen3가 응답을 생성하지 않으므로 user 트리거 추가
    if not full_messages or full_messages[-1].get("role") != "user":
        full_messages.append({"role": "user", "content": "/no_think"})

    full_reply_holder = []

    def generate():
        try:
            stream = llm.stream_chatbot(full_messages)
            for text in _iter_stream_content(stream):
                full_reply_holder.append(text)
                yield f"data: {json.dumps({'delta': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            return

        full_reply = "".join(full_reply_holder)
        if full_reply:
            save_turn(session_id, f"[챗봇 유도: {reason}]", full_reply, code_context)
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

    # evidence 검증 기준이 되는 학생 발화 원문
    student_text = " ".join(
        m.get("content", "") for m in chat_history if m.get("role") == "user"
    )

    try:
        # CT 분석: 파싱 실패 시 1회 재시도 (temp 0.7로 간헐적 JSON 깨짐 대비)
        ct_eval = None
        for _ in range(2):
            try:
                ct_eval = parse_ct_evaluation(llm.call_log_analysis(log_text), student_text)
                break
            except (ValueError, json.JSONDecodeError):
                continue
        if ct_eval is None:
            raise ValueError("CT 분석 JSON 파싱 실패 (재시도 후)")
        pr_raw  = _extract_json(llm.call_prompt_eval(log_text))

        detail         = ct_eval["detail"]
        weak_ct        = ct_eval["weak_ct"]
        ct_total       = ct_eval["ct_total"]
        observed_count = ct_eval["observed_count"]

        # 지표별 점수(미관찰 null) — 시작화면 막대용
        ct_scores = {k: detail[k]["score"] for k in CT_SKILLS}

        # 0~100 환산 (기존 링 UI 호환): 관찰된 지표의 만점 대비
        ct_score = round(ct_total / (observed_count * 3) * 100) if observed_count else 0

        # 대표 피드백: 가장 약한 관찰 지표의 피드백 (없으면 안내)
        if weak_ct:
            ct_feedback = f"[{weak_ct}] {detail[weak_ct]['feedback']}"
        else:
            ct_feedback = "대화 근거가 적어 평가하기 어려웠어요. 다음엔 코드에 대해 더 질문해볼까요?"

        # 프롬프팅 6요소 점수 정규화 (보조 지표)
        prompt_scores = {k: max(1, min(5, int(pr_raw.get(k, 1)))) for k in PROMPT_SKILLS}
        prompt_score  = round(sum(prompt_scores.values()) / len(prompt_scores) * 20)

        # feedback.json 저장 (세션당 1회)
        save_feedback(
            session_id, topic,
            {"ct_scores": ct_scores, "ct_detail": detail, "ct_total": ct_total,
             "observed_count": observed_count, "weak_ct": weak_ct, "feedback": ct_feedback},
            {**prompt_scores, "overall": prompt_score, "feedback": pr_raw.get("feedback", "")},
        )

        return jsonify({
            "ct_scores":       ct_scores,        # 지표별 점수(미관찰 null) — 막대 UI
            "ct_evaluation":   detail,           # 지표별 observed/score/evidence/feedback
            "ct_total":        ct_total,         # 코드 합산 (0~21)
            "observed_count":  observed_count,
            "weak_ct":         weak_ct,
            "ct_score":        ct_score,          # 0~100 (기존 링 UI 호환)
            "ct_feedback":     ct_feedback,
            "prompt_scores":   prompt_scores,     # 6요소 각 점수
            "prompt_score":    prompt_score,
            "prompt_feedback": pr_raw.get("feedback", ""),
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
