from flask import Blueprint, request, jsonify, Response, stream_with_context
from openai import OpenAI
import json
import re
import os
from datetime import datetime
import rag as rag_store

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

client = OpenAI(
    base_url="http://localhost:1234/v1",
    api_key="lm-studio",
)

SYSTEM_CODE = (
    "You are a coding tutor for beginners. "
    "Return ONLY a runnable Python code block. "
    "No comments, no explanation, no markdown fences."
)

SYSTEM_PROBLEM = (
    "You are a coding tutor. "
    "Write exactly ONE learning problem in Korean based on the given code. "
    "Output ONLY the problem sentence. No numbering, no title, no explanation."
)

SYSTEM_CHAT = (
    "You are a helpful coding assistant for learners of computational thinking. "
    "Answer questions in Korean clearly and concisely. "
    "When explaining code, use simple language."
)

DIFFICULTIES = ["매우 쉬움", "쉬움", "보통", "어려움", "매우 어려움"]

# 한국어 문장 종결 패턴
_KO_ENDING = re.compile(r"[가-힣]+(?:세요|요\?|까요\?|인가요\?|볼까요\?|보세요\.?|해요\.?|십시오\.?)\s*$")


def _is_mostly_korean(text: str) -> bool:
    ko = len(re.findall(r"[가-힣]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return ko > 0 and ko >= en


def _extract_from_reasoning(reasoning: str) -> str:
    """reasoning_content에서 한국어 문제 문장 하나를 추출."""
    candidates = []
    for line in reasoning.splitlines():
        line = line.strip().lstrip("*- ").strip()
        # 레이블 뒤 텍스트 추출 (Draft: ..., Refined: ... 등)
        m = re.search(
            r"(?:Better Draft|Final Draft[^:]*|Revised[^:]*|Refinement|Draft)\s*:?\*?\s*(.+)",
            line, re.IGNORECASE,
        )
        text = m.group(1).strip().lstrip("*").strip() if m else line

        if len(text) < 10:
            continue
        # 한국어가 주를 이루는 문장만 수집
        if _is_mostly_korean(text):
            candidates.append(text)

    # 마지막(가장 정제된) 후보 반환
    for text in reversed(candidates):
        if _KO_ENDING.search(text) or "?" in text:
            return text
    return candidates[-1] if candidates else ""


def call_lm(system: str, user_content: str, max_tokens: int = 1024) -> str:
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
    choice = response.choices[0]
    content = (choice.message.content or "").strip()

    if not content:
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        content = _extract_from_reasoning(reasoning)

    return content


def strip_fences(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text.strip())
    return text.strip()


@api.route("/status", methods=["GET"])
def status():
    try:
        models = client.models.list()
        return jsonify({"connected": True, "models": [m.id for m in models.data]})
    except Exception:
        return jsonify({"connected": False, "models": []})


@api.route("/generate-code", methods=["POST"])
def generate_code():
    data = request.get_json()
    topic = data.get("topic", "Python 기초")
    try:
        ctx = rag_store.retrieve("code_examples", topic)
        if ctx:
            user_msg = (
                f"/no_think [참고 자료]\n{ctx}\n\n"
                f"위 참고 자료를 활용해서 '{topic}'을 보여주는 예제 코드를 작성해줘."
            )
        else:
            user_msg = f"/no_think '{topic}'을 보여주는 간단한 예제 코드를 작성해줘."

        raw = call_lm(SYSTEM_CODE, user_msg)
        return jsonify({"code": strip_fences(raw)})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@api.route("/generate-problem", methods=["POST"])
def generate_problem():
    data = request.get_json()
    code = data.get("code", "")
    problem_index = data.get("problem_index", 0)
    previous = data.get("previous_problems", [])

    difficulty = DIFFICULTIES[min(problem_index, 4)]

    ctx = rag_store.retrieve("problem_templates", f"{difficulty} {code[:300]}")
    ctx_block = f"\n\n[문제 참고 템플릿]\n{ctx}" if ctx else ""

    prev_text = ""
    if previous:
        prev_text = "\n\n이미 출제된 문제 (중복 금지):\n" + "\n".join(f"- {p}" for p in previous)

    prompt = (
        f"/no_think 아래 Python 코드를 보고 학습 문제를 한국어로 딱 1개만 만들어줘.\n"
        f"난이도: {difficulty} (총 5문제 중 {problem_index + 1}번째)\n"
        f"문제 내용만 한 문장으로 출력해. 번호나 제목은 쓰지 마."
        f"{ctx_block}{prev_text}\n\n코드:\n{code}"
    )

    try:
        raw = call_lm(SYSTEM_PROBLEM, prompt, max_tokens=4096)
        problem = ""
        for line in raw.splitlines():
            line = re.sub(r"^\d+[\.\)\-\:]?\s*", "", line).strip()
            if line:
                problem = line
                break
        if not problem:
            problem = raw.strip()
        return jsonify({"problem": problem})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@api.route("/rag/status", methods=["GET"])
def rag_status():
    """RAG 상태 + 파일 목록 + 벡터 수 반환."""
    return jsonify({
        "available": rag_store._AVAILABLE,
        "embed_model": rag_store.EMBED_MODEL,
        "docs_dir": rag_store.DOCS_DIR,
        "counts": rag_store.counts(),
        "files": rag_store.list_docs(),
    })


@api.route("/rag/rebuild", methods=["POST"])
def rag_rebuild():
    """rag_docs/ 를 다시 스캔해서 인덱스 재빌드. JSON: {collection?} (없으면 전체)"""
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
    data = request.get_json()
    history = data.get("messages", [])
    session_id = data.get("session_id", "unknown")
    code_context = data.get("code_context")

    messages = [{"role": "system", "content": SYSTEM_CHAT}] + history
    full_reply_holder = []

    def generate():
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

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


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
