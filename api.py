from flask import Blueprint, request, jsonify, Response, stream_with_context
import json
import random
import re
import os
from datetime import datetime
import rag as rag_store
import llm

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

FEEDBACK_FILE = os.path.join(os.path.dirname(__file__), "feedback.json")

CT_MAP = {
    0: {"ct_skill": "분해"},
    1: {"ct_skill": "패턴인식"},
    2: {"ct_skill": "추상화"},
    3: {"ct_skill": "알고리즘적사고"},
    4: {"ct_skill": "통합"},
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
    data = request.get_json()
    code = data.get("code", "")
    problem_index = data.get("problem_index", 0)
    previous = data.get("previous_problems", [])

    ct_info    = CT_MAP[min(problem_index, 4)]
    ct_skill   = ct_info["ct_skill"]
    difficulty = ""

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

    system_prompt = llm.build_chat_system(code_context, current_problem)

    if trigger in ("hint", "explain"):
        trigger_msg = llm.build_trigger_user_message(trigger)
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
