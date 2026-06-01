from flask import Blueprint, request, jsonify, Response, stream_with_context
from openai import OpenAI
import json
import re
import os
from datetime import datetime
import rag as rag_store

api = Blueprint("api", __name__, url_prefix="/api")

CONVERSATIONS_FILE = os.path.join(os.path.dirname(__file__), "conversations.json")

LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
LM_STUDIO_MODEL    = "local-model"  # LM Studio에 로드된 모델 자동 참조

client = OpenAI(
    base_url=LM_STUDIO_BASE_URL,
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

SYSTEM_CHAT_BASE = (
    "You are a helpful coding assistant for learners of computational thinking. "
    "Answer questions in Korean clearly and concisely. "
    "When explaining code, use simple language. "
    "If the user's question relates to the current code or problem, refer to them directly in your answer."
)


def build_chat_system(code_context: str = None, current_problem: str = None) -> str:
    system = SYSTEM_CHAT_BASE
    if code_context:
        system += f"\n\n[현재 학습 중인 코드]\n{code_context}"
    if current_problem:
        system += f"\n\n[현재 풀고 있는 문제]\n{current_problem}"
    return system

DIFFICULTIES = ["매우 쉬움", "쉬움", "보통", "어려움", "매우 어려움"]

CT_SKILL_MAP = {
    0: "분해",
    1: "패턴인식",
    2: "추상화",
    3: "알고리즘적사고",
    4: "통합",
}

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


def call_lm(system: str, user_content: str, max_tokens: int = 4096) -> str:
    response = client.chat.completions.create(
        model=LM_STUDIO_MODEL,
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
    ct_skill = CT_SKILL_MAP[min(problem_index, 4)]

    ctx = rag_store.retrieve("problem_templates", f"{ct_skill} {difficulty} {code[:300]}")
    ctx_block = f"\n\n[참고 템플릿]\n{ctx}" if ctx else ""

    prev_text = ""
    if previous:
        prev_text = "\n\n[이미 출제된 문제 - 중복 금지]\n" + "\n".join(f"- {p}" for p in previous)

    prompt = (
        f"/no_think 다음 파이썬 코드를 읽고 {ct_skill} 능력을 측정하는 객관식 문제 1개를 만들어라.\n\n"
        f"[CT 요소: {ct_skill}]\n"
        f"[난이도: {difficulty}] (총 5문제 중 {problem_index + 1}번째)"
        f"{ctx_block}"
        f"{prev_text}\n\n"
        f"[파이썬 코드]\n{code}\n\n"
        f"규칙:\n"
        f"- 보기 4개 (A/B/C/D)\n"
        f"- 정답과 해설 포함\n"
        f"- {ct_skill}을 측정하는 질문 방향으로 출제\n"
        f"- 첫 줄에 문제 질문만 출력"
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
        return jsonify({"problem": problem, "ct_skill": ct_skill})
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
    current_problem = data.get("current_problem")

    messages = [{"role": "system", "content": build_chat_system(code_context, current_problem)}] + history
    full_reply_holder = []

    def generate():
        try:
            stream = client.chat.completions.create(
                model=LM_STUDIO_MODEL,
                messages=messages,
                max_tokens=4096,
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


SYSTEM_EVAL = (
    "You are an educational assessment AI. "
    "Evaluate the student's computational thinking and prompting quality based on their answers and chat history. "
    "Respond ONLY with valid JSON. No other text."
)


@api.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.get_json()
    topic = data.get("topic", "")
    code = data.get("code", "")
    problems = data.get("problems", [])
    answers = data.get("answers", [])
    chat_history = data.get("chat_history", [])

    qa_pairs = ""
    for i, (p, a) in enumerate(zip(problems, answers), 1):
        qa_pairs += f"\n{i}. [문제] {p}\n   [답변] {a}\n"

    chat_text = ""
    for msg in chat_history:
        role = "학생" if msg["role"] == "user" else "AI"
        chat_text += f"\n{role}: {msg['content']}"

    prompt = (
        f"/no_think 다음은 Python '{topic}' 주제 학습 중 학생의 문제 답변과 챗봇 대화 내역입니다.\n\n"
        f"코드 예제:\n{code}\n\n"
        f"문제와 답변:{qa_pairs}\n"
        f"챗봇 대화:{chat_text if chat_text else ' (없음)'}\n\n"
        f"위 내역을 바탕으로 아래 두 가지를 평가해주세요.\n\n"
        f"1. 컴퓨팅 사고력: 분해(Decomposition), 패턴인식(Pattern Recognition), 추상화(Abstraction), 알고리즘(Algorithm) 측면에서 평가\n"
        f"2. 프롬프팅 품질: 질문의 명확성, 구체성, 맥락 제공 정도를 평가\n\n"
        f"반드시 아래 JSON 형식만 출력하세요. 다른 텍스트 없이 JSON만 출력.\n"
        f'{{"ct_score": 숫자(0~100), "ct_feedback": "한국어 피드백", "prompt_score": 숫자(0~100), "prompt_feedback": "한국어 피드백"}}'
    )

    try:
        raw = call_lm(SYSTEM_EVAL, prompt, max_tokens=1024)
        m = re.search(r'\{[^{}]+\}', raw, re.DOTALL)
        if m:
            result = json.loads(m.group())
        else:
            result = json.loads(raw.strip())
        return jsonify(result)
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
