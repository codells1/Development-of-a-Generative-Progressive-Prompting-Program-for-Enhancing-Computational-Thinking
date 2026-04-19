from flask import Blueprint, request, jsonify
import requests
import json
import os
from datetime import datetime

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = "conversations.json"

def save_turn(session_id, user_message, assistant_reply, code_context=None):
    try:
        if os.path.exists(CONVERSATIONS_FILE):
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                sessions = json.load(f)
        else:
            sessions = []

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        turn = {
            "turn_index": None,
            "timestamp": now,
            "user": user_message,
            "assistant": assistant_reply,
        }

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

LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"

def call_lm(messages, max_tokens=1024):
    payload = {
        "model": "local-model",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }
    try:
        res = requests.post(LM_STUDIO_URL, json=payload, timeout=60)
        res.raise_for_status()
        return res.json()["choices"][0]["message"]["content"]
    except requests.exceptions.ConnectionError:
        raise RuntimeError("LM Studio에 연결할 수 없습니다. LM Studio를 실행하고 서버를 시작해주세요.")

@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json()
    topic = data.get("topic", "Python 기초")
    quadrant = data.get("quadrant", "q2")
    messages = [
        {"role": "system", "content": "You are a coding tutor. Return only a clean code block with brief inline comments in Korean. No extra explanation."},
        {"role": "user", "content": f"'{topic}'을 보여주는 간단한 예제 코드를 작성해줘."},
    ]
    try:
        code = call_lm(messages)
        return jsonify({"code": code, "quadrant": quadrant})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    data = request.get_json()
    code = data.get("code", "")
    messages = [
        {"role": "system", "content": "You are a coding tutor. Create 3 short learning problems in Korean based on the given code."},
        {"role": "user", "content": f"다음 코드를 보고 학습 문제 3개를 만들어줘:\n\n{code}"},
    ]
    try:
        problem = call_lm(messages)
        return jsonify({"problem": problem})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503

@api.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    history = data.get("messages", [])
    session_id = data.get("session_id", "unknown")
    code_context = data.get("code_context")
    messages = [{"role": "system", "content": "You are a helpful coding assistant. Answer in Korean."}] + history
    try:
        reply = call_lm(messages)
        user_message = history[-1]["content"] if history else ""
        save_turn(session_id, user_message, reply, code_context)
        return jsonify({"reply": reply})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
