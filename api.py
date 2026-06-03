from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import re
import os
from datetime import datetime
import rag as rag_store
import llm

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

STAGE_MAP = {
    0: "변수·연산·조건",
    1: "반복·리스트",
    2: "함수",
    3: "알고리즘",
}

FUSION_TOPIC = "융합"

CT_SKILLS     = ("분해", "패턴인식", "추상화", "알고리즘적사고")
PROMPT_SKILLS = ("명확성", "구체성", "맥락제공", "관련성", "자기주도성", "발전성")

FEEDBACK_FILE = os.path.join(os.path.dirname(__file__), "feedback.json")

CT_MAP = {
    0: {"ct_skill": "분해",           "difficulty": "매우쉬움"},
    1: {"ct_skill": "패턴인식",       "difficulty": "쉬움"},
    2: {"ct_skill": "추상화",         "difficulty": "보통"},
    3: {"ct_skill": "알고리즘적사고", "difficulty": "어려움"},
    4: {"ct_skill": "통합",           "difficulty": "매우어려움"},
}


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


def save_feedback(session_id: str, topic: str, ct: dict, prompt: dict) -> None:
    """CT 4요소 점수 + 프롬프팅 6요소 점수를 feedback.json에 누적 저장한다."""
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
            "ct_scores":       {k: ct.get(k, 1) for k in CT_SKILLS},
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

    messages = [{"role": "system", "content": llm.build_chat_system(code_context, current_problem, weak_ct)}] + history
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
        user_message = history[-1]["content"] if history else ""
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

    system = llm.build_nudge_system(code_context, current_problem, reason, weak_ct)
    full_messages = [{"role": "system", "content": system}] + messages
    full_reply_holder = []

    def generate():
        try:
            stream = llm.stream_chatbot(full_messages)
            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_reply_holder.append(delta)
                    yield f"data: {json.dumps({'delta': delta})}\n\n"
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

    try:
        ct_raw = _extract_json(llm.call_log_analysis(log_text))
        pr_raw = _extract_json(llm.call_prompt_eval(log_text))

        # CT 4요소 점수 정규화 (1~5 클램핑)
        ct_scores = {k: max(1, min(5, int(ct_raw.get(k, 1)))) for k in CT_SKILLS}

        # weak_ct 보정: LLM 산출값이 유효 키가 아니면 최솟값 키로 재계산
        weak_ct = ct_raw.get("weak_ct", "")
        if weak_ct not in ct_scores:
            weak_ct = min(ct_scores, key=ct_scores.get)

        # 평균(1~5) → 100점 환산 (프론트 현행 호환)
        ct_score = round(sum(ct_scores.values()) / len(ct_scores) * 20)

        # 프롬프팅 6요소 점수 정규화
        prompt_scores = {k: max(1, min(5, int(pr_raw.get(k, 1)))) for k in PROMPT_SKILLS}
        prompt_score  = round(sum(prompt_scores.values()) / len(prompt_scores) * 20)

        # feedback.json 저장
        save_feedback(
            session_id, topic,
            {**ct_scores, "weak_ct": weak_ct, "feedback": ct_raw.get("feedback", "")},
            {**prompt_scores, "overall": prompt_score, "feedback": pr_raw.get("feedback", "")},
        )

        return jsonify({
            "ct_scores":       ct_scores,          # 4요소 각 점수 (Task 7에서 UI 활용)
            "weak_ct":         weak_ct,
            "ct_score":        ct_score,            # 현 프론트용 단일 점수
            "ct_feedback":     ct_raw.get("feedback", ""),
            "prompt_scores":   prompt_scores,       # 6요소 각 점수
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
