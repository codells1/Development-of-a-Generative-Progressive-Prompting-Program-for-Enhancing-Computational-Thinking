from flask import Blueprint, request, jsonify, Response, stream_with_context
from openai import OpenAI
import json
import os
from datetime import datetime

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

# 참조: tetrapod0/RAG_with_lm_studio 방식
# OpenAI 호환 클라이언트로 LM Studio 연결
client = OpenAI(
    base_url="http://localhost:1234/v1",
    api_key="lm-studio",
)

# ── 각 섹션별 시스템 프롬프트 ────────────────────────────
SYSTEM_CODE = (
    "You are a coding tutor for beginners. "
    "Return ONLY a runnable Python code block with brief Korean inline comments. "
    "No explanation outside the code."
)

SYSTEM_PROBLEM = (
    "You are a coding tutor. "
    "Create exactly 5 progressive learning problems in Korean based on the given code. "
    "Problems should range from easy to hard. "
    "Return ONLY a JSON array of 5 strings. "
    'Example: ["문제1", "문제2", "문제3", "문제4", "문제5"]'
)

SYSTEM_CHAT = (
    "You are a helpful coding assistant for learners of computational thinking. "
    "Answer questions in Korean clearly and concisely. "
    "When explaining code, use simple language."
)

# ── LM Studio 단순 호출 (코드/문제 생성용) ───────────────
def call_lm(system, user_content, max_tokens=1024):
    response = client.chat.completions.create(
        model="local-model",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        max_tokens=max_tokens,
        temperature=0.7,
        stream=False,
    )
    return response.choices[0].message.content

# ── 연결 상태 확인 ────────────────────────────────────────
@api.route("/status", methods=["GET"])
def status():
    try:
        models = client.models.list()
        model_ids = [m.id for m in models.data]
        return jsonify({"connected": True, "models": model_ids})
    except Exception:
        return jsonify({"connected": False, "models": []})

# ── 코드 섹션: 예제 코드 생성 ─────────────────────────────
@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json()
    topic = data.get("topic", "Python 기초")
    try:
        code = call_lm(SYSTEM_CODE, f"'{topic}'을 보여주는 간단한 예제 코드를 작성해줘.")
        return jsonify({"code": code})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

# ── 문제 섹션: 학습 문제 5개 생성 ────────────────────────
@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    data = request.get_json()
    code = data.get("code", "")
    try:
        raw = call_lm(SYSTEM_PROBLEM, f"다음 코드를 보고 학습 문제 5개를 JSON 배열로 만들어줘:\n\n{code}")
        start, end = raw.find("["), raw.rfind("]")
        problems = json.loads(raw[start:end + 1]) if start != -1 else [raw]
        return jsonify({"problems": problems[:5]})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

# ── 챗봇 섹션: 스트리밍 응답 ──────────────────────────────
@api.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    history = data.get("messages", [])
    session_id = data.get("session_id", "unknown")
    code_context = data.get("code_context")

    messages = [{"role": "system", "content": SYSTEM_CHAT}] + history

    def generate():
        full_reply = ""
        try:
            stream = client.chat.completions.create(
                model="local-model",
                messages=messages,
                max_tokens=1024,
                temperature=0.7,
                stream=True,
            )
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

    full_reply_holder = []
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ── 대화 저장 ─────────────────────────────────────────────
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
