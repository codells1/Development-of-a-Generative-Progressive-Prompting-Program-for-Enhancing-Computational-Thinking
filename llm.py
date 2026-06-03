"""
llm.py — 모든 LLM 호출을 역할별로 분리·관리하는 중앙 모듈
"""

import re
import random
import threading
from openai import OpenAI

BASE_URL = "http://localhost:1234/v1"
MODEL    = "local-model"

TEMP_CREATIVE = 0.6   # 챗봇, 문제 생성
TEMP_CODE     = 0.8   # 코드 생성 — 매번 다른 출력 유도
TEMP_PRECISE  = 0.2   # CT 분석, 프롬프트 평가

_client   = OpenAI(base_url=BASE_URL, api_key="lm-studio")
_llm_lock = threading.Lock()


# ── System Prompts ──────────────────────────────────────────────────

SYSTEM_CHAT = (
    "당신은 컴퓨팅 사고력(CT) 학습을 돕는 소크라테스식 교육 AI다.\n"
    "학생이 스스로 답을 발견하도록 유도 질문으로 대화한다.\n\n"
    "절대 금지:\n"
    "- 정답, 정답 코드, 정답 값, 구체적 수치를 직접 알려주지 않는다.\n"
    "- 'A번이 맞습니다', '정답은 ~입니다' 같은 직접 답변 금지.\n\n"
    "대화 유형별 대응:\n"
    "1. 학생이 이해한 내용이나 자신의 생각을 표현할 때\n"
    "   - 판단 근거는 현재 학습 중인 코드와 문제(아래에 제공됨)를 사용한다.\n"
    "   - 먼저 맞는지 틀린지 짧게 피드백한다(예: '맞아요!', '흠, 조금 다르게 생각해볼까요?').\n"
    "   - 맞으면: 짧게 긍정 확인 후 더 깊은 질문으로 확장한다.\n"
    "   - 틀리면: 부드럽게 틀렸다는 신호를 주고, 코드의 어느 부분을 다시 볼지 질문으로 유도한다.\n"
    "   - 어떤 경우에도 정답이나 구체적 값은 직접 제공하지 않는다.\n"
    "2. 학생이 질문을 할 때\n"
    "   - 기존 소크라테스 방식대로 유도 질문으로 답한다. 정답 방향만 제시하고 직접 답변 금지.\n\n"
    "반드시 지킬 사항:\n"
    "- 모든 답변은 질문으로 끝낸다. 예: '그렇다면 x가 1일 때 어떻게 될까요?'\n"
    "- 코드의 어느 부분을 보면 힌트가 되는지 방향만 제시한다.\n"
    "- 한국어로 답변한다. 중학생도 이해할 수 있는 쉬운 말을 쓴다.\n"
    "- 학생 질문이 모호하면 어느 부분을 묻는지 되묻는다."
)

SYSTEM_CODE = (
    "You are a coding tutor for beginners. "
    "Return ONLY a runnable Python code block. "
    "No comments, no explanation, no markdown fences.\n"
    "The reference example is for style and difficulty reference ONLY. "
    "NEVER copy it directly — change variable names, situation, and numbers to write a brand-new original code."
)

SYSTEM_PROBLEM = (
    "You are a coding tutor. Create exactly ONE multiple-choice question in Korean "
    "based on the given Python code.\n"
    "CRITICAL RULES — violating any of these is forbidden:\n"
    "1. Ask ONLY about syntax, variables, or logic that ACTUALLY EXISTS in the code.\n"
    "2. Do NOT invent situations or judgments absent from the code "
    "(e.g. do not ask about speeding when the code only calculates distance).\n"
    "3. The correct answer MUST be derivable by actually running or tracing the code — "
    "not by inference or assumption.\n"
    "4. Do NOT put variables, values, or concepts in any option that do not appear in the code.\n"
    "Output ONLY valid JSON — no markdown fences, no other text:\n"
    '{"question": "문제 본문", '
    '"options": ["A. 보기1", "B. 보기2", "C. 보기3", "D. 보기4"], '
    '"answer": "B", '
    '"verification_code": "...", '
    '"explanation": "정답 해설 1~2문장", '
    '"ct_skill": "CT요소명", '
    '"difficulty": "난이도명"}\n'
    "verification_code rules:\n"
    "- Write a STANDALONE Python snippet that prints ONLY the answer value to stdout.\n"
    "- Copy all required variable definitions from the original code into this snippet.\n"
    "- Example: if the question asks the output of `total = sum([1,2,3]); print(total)`, "
    'write verification_code = "total = sum([1,2,3])\\nprint(total)"\n'
    "- For conceptual questions where the answer cannot be computed by running code "
    "(e.g. abstraction, decomposition type), set verification_code to empty string \"\".\n"
    "Rules: exactly 4 options labeled A/B/C/D, answer is one capital letter, "
    "wrong options are plausible but clearly incorrect, vary correct answer position."
)

SYSTEM_CT_ANALYSIS = (
    "You are an educational assessment AI.\n"
    "Analyze ONLY the student's chat messages to evaluate their computational thinking (CT).\n"
    "Do NOT base scores on quiz answer correctness — focus on HOW the student asked questions.\n\n"
    "CT Rubric (score each element 1~5):\n"
    "- 분해(1~5): 문제를 역할별 부분으로 나눠 질문했는가\n"
    "  1=질문 없음, 3=일부 분해 시도, 5=체계적 분해 질문\n"
    "- 패턴인식(1~5): 반복·규칙성을 발견하는 질문을 했는가\n"
    "  1=없음, 3=패턴 언급, 5=규칙을 스스로 발견하는 질문\n"
    "- 추상화(1~5): 변수·함수의 역할을 일반화해 사고했는가\n"
    "  1=없음, 3=의미 질문, 5=일반화·상위 개념 연결\n"
    "- 알고리즘적사고(1~5): 실행 순서·흐름을 추적하는 질문을 했는가\n"
    "  1=없음, 3=순서 질문, 5=흐름을 단계별로 추적\n\n"
    "weak_ct: 가장 낮은 점수의 CT 요소명. 동점이면 학습 효과가 큰 요소 선택.\n"
    "If no student chat messages exist, score all elements 1.\n\n"
    'Respond ONLY with valid JSON (no markdown, no extra text):\n'
    '{"분해": 점수, "패턴인식": 점수, "추상화": 점수, "알고리즘적사고": 점수, '
    '"weak_ct": "요소명", "feedback": "한국어 피드백 2~3문장"}'
)

SYSTEM_PROMPT_EVAL = (
    "You are an educational assessment AI.\n"
    "Analyze ONLY the student's chat messages and score their computational thinking "
    "with the rubric below. Do NOT use quiz answer correctness — focus on HOW the "
    "student questioned and reasoned.\n\n"
    "CT Rubric — score each indicator 1(미흡) / 2(보통) / 3(우수):\n"
    "- 문제분해: 1=코드를 통째로 '이게 다 뭐 하는지/전부 해결해줘'식으로 질문. "
    "2=함수·블록 단위로 나눠 묻지만 부분 간 연결(입출력·순서) 고려 없음. "
    "3=의미 단위로 나누고 각 부분의 입력·출력과 부분 간 순서·관계까지 고려해 질문.\n"
    "- 용어사용: 1='돌아가는 거/그거'처럼 일상어만 쓰고 프로그래밍 용어 안 씀. "
    "2=용어를 쓰지만 부정확(예: 변수를 함수라 부름)하거나 일상어와 섞음. "
    "3=변수·함수·매개변수·반환값 등 핵심 용어를 의미에 맞게 정확히 사용.\n"
    "- 추상화: 1=모든 줄을 똑같이 보고 핵심 로직과 부수 코드를 구분 못 함. "
    "2=핵심은 짚지만 부수적 부분(초기화·출력형식)과 핵심 로직의 경계가 흐림. "
    "3=핵심 로직과 보조 코드를 명확히 구분하고 그 코드가 결국 무엇을 하는지 한 문장으로 요약.\n"
    "- 실행흐름: 1=실행 순서를 안 따지고 결과·답만 요청. "
    "2=처리 순서를 대략 설명하나 조건 분기·반복의 시작/종료에서 흐름을 놓침. "
    "3=순차·조건·반복 구조를 따라 실행 순서를 정확히 추적하며 특정 지점의 흐름을 질문.\n"
    "- 자료표현: 1=데이터(입력·출력·변수) 언급 없이 결과만 요청. "
    "2=데이터 형태(숫자·문자 등)나 변수 쓰임은 언급하나 자료구조·타입을 정확히 못 짚음. "
    "3=변수 타입과 자료구조(리스트·딕셔너리 등)를 정확히 짚고 그 선택 이유까지 설명.\n"
    "- 동작확인: 1=AI가 준 코드를 왜 그런지 안 따지고 그대로 씀. "
    "2=출력 결과엔 관심 있으나 왜 그렇게 동작하는지는 안 물음. "
    "3=각 부분이 왜 필요하고 어떻게 동작하는지 구체적으로 묻고, 결과가 기대와 다르면 문제 지점을 짚어 수정 방향을 요청.\n"
    "- 자기해결: 1=스스로 시도 없이 '정답이 뭐예요'식으로 답만 요구. "
    "2=힌트를 요청하나 받은 힌트를 스스로 적용해 보지 않고 다시 답을 요구하거나 멈춤. "
    "3=힌트를 받아 스스로 적용·확인하고 나아가 자신의 아이디어를 담아 문제 해결을 시도.\n\n"
    "If no student chat messages exist, score every indicator 1.\n\n"
    "Respond ONLY with valid JSON (no markdown, no extra text):\n"
    '{"문제분해": 점수, "용어사용": 점수, "추상화": 점수, "실행흐름": 점수, '
    '"자료표현": 점수, "동작확인": 점수, "자기해결": 점수, "feedback": "한국어 피드백 2~3문장"}'
)


# ── Reasoning Fallback (Qwen3 thinking mode) ───────────────────────

_KO_ENDING = re.compile(
    r"[가-힣]+(?:세요|요\?|까요\?|인가요\?|볼까요\?|보세요\.?|해요\.?|십시오\.?)\s*$"
)


def _is_mostly_korean(text: str) -> bool:
    ko = len(re.findall(r"[가-힣]", text))
    en = len(re.findall(r"[a-zA-Z]", text))
    return ko > 0 and ko >= en


def _extract_from_reasoning(reasoning: str) -> str:
    candidates = []
    for line in reasoning.splitlines():
        line = line.strip().lstrip("*- ").strip()
        m = re.search(
            r"(?:Better Draft|Final Draft[^:]*|Revised[^:]*|Refinement|Draft)\s*:?\*?\s*(.+)",
            line, re.IGNORECASE,
        )
        text = m.group(1).strip().lstrip("*").strip() if m else line
        if len(text) < 10:
            continue
        if _is_mostly_korean(text):
            candidates.append(text)
    for text in reversed(candidates):
        if _KO_ENDING.search(text) or "?" in text:
            return text
    return candidates[-1] if candidates else ""


# ── Single Entry Point ─────────────────────────────────────────────

def _generate(messages: list, temperature: float, max_tokens: int = 4096) -> str:
    """모든 비스트리밍 LLM 호출의 단일 진입점. Lock으로 동시 호출을 직렬화한다."""
    with _llm_lock:
        resp = _client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
    choice = resp.choices[0]
    content = (choice.message.content or "").strip()
    # Qwen3가 content에 <think>...</think> 블록을 포함할 경우 제거
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if not content:
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        content = _extract_from_reasoning(reasoning)
    return content


_CODE_SITUATIONS = [
    "쇼핑 장바구니", "시험 점수", "운동 기록",
    "온도 측정", "게임 점수", "도서 대출", "용돈 관리",
]


# ── 생성 계열 (TEMP_CREATIVE, history 없음) ────────────────────────

def call_code_gen(topic: str, ctx: str = "") -> str:
    situation = random.choice(_CODE_SITUATIONS)
    ref_block = f"\n\n[스타일 참고 — 코드 복사 금지]\n{ctx}" if ctx else ""
    user_content = (
        f"/no_think\n"
        f"[주제] {topic}\n"
        f"[상황] {situation}\n\n"
        f"반드시 '{situation}' 맥락으로 '{topic}'을 보여주는 완전히 새로운 코드를 작성하라. "
        f"변수명·숫자·상황이 아래 참고 예제와 달라야 한다. 절대 복사 금지."
        f"{ref_block}"
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_CODE},
         {"role": "user",   "content": user_content}],
        TEMP_CODE,
    )


def call_code_gen_fusion(ctx: str = "") -> str:
    situation = random.choice(_CODE_SITUATIONS)
    ref_block = f"\n\n[스타일 참고 — 코드 복사 금지]\n{ctx}" if ctx else ""
    user_content = (
        f"/no_think\n"
        f"[주제] 융합\n"
        f"[상황] {situation}\n\n"
        f"반드시 '{situation}' 맥락으로 여러 프로그래밍 개념(변수·조건, 반복·리스트, 함수, 알고리즘)을 "
        f"2개 이상 결합한 완전히 새로운 종합 코드를 작성하라. "
        f"변수명·숫자·상황이 아래 참고 예제와 달라야 한다. 절대 복사 금지."
        f"{ref_block}"
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_CODE},
         {"role": "user",   "content": user_content}],
        TEMP_CODE,
    )


def call_problem_gen(
    code: str,
    ct_skill: str,
    difficulty: str,
    templates: str,
    previous_problems: list,
    problem_index: int = 0,
) -> str:
    """CT 요소 기반 4지선다 객관식 문제 생성. JSON 반환."""
    ctx_block = (
        f"\n\n[참고 템플릿(형식·질문방식 참고용 — 내용 복사 금지)]\n"
        f"※ 아래 템플릿은 문제 형식과 질문 방식만 참고하는 용도다. 내용은 무시하라.\n"
        f"⚠ 반드시 위 [파이썬 코드]에 실제로 존재하는 문법·변수·로직만으로 출제하라.\n"
        f"⚠ 코드에 없는 문법이나 코드에 없는 상황·판단·기능을 지어내지 마라.\n"
        f"⚠ 문제의 정답은 코드를 실제로 실행하거나 추적해서 나오는 결과여야 한다.\n"
        f"⚠ 코드에 등장하지 않는 변수·값·개념을 보기에 넣지 마라.\n"
        f"{templates}"
    ) if templates else ""
    prev_text = (
        "\n\n[이미 출제된 문제 - 중복 금지]\n"
        + "\n".join(f"- {p}" for p in previous_problems)
    ) if previous_problems else ""
    user_content = (
        f"/no_think [CT 요소: {ct_skill}]\n"
        f"[난이도: {difficulty}] (총 5문제 중 {problem_index + 1}번째)\n\n"
        f"[파이썬 코드]\n{code}"
        f"{ctx_block}{prev_text}"
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_PROBLEM},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
        max_tokens=1024,
    )


# ── 분석 계열 (TEMP_PRECISE, history 미주입) ────────────────────────

def call_log_analysis(log_text: str) -> str:
    """CT 측정. 대화 로그를 분석 대상 자료로만 전달 (history 미주입)."""
    return _generate(
        [{"role": "system", "content": SYSTEM_CT_ANALYSIS},
         {"role": "user",   "content": f"/no_think [분석할 학습 기록]\n{log_text}"}],
        TEMP_PRECISE,
        max_tokens=512,
    )


def call_prompt_eval(log_text: str) -> str:
    """프롬프팅 품질 평가. 대화 로그를 분석 대상 자료로만 전달 (history 미주입)."""
    return _generate(
        [{"role": "system", "content": SYSTEM_PROMPT_EVAL},
         {"role": "user",   "content": f"/no_think [분석할 학습 기록]\n{log_text}"}],
        TEMP_PRECISE,
        max_tokens=512,
    )


# ── 챗봇 (스트리밍, Lock 미적용) ────────────────────────────────────

_CT_GUIDANCE = {
    "분해":          "코드를 역할별 부분으로 나눠 생각하도록",
    "패턴인식":      "반복되는 패턴·규칙을 발견하도록",
    "추상화":        "변수·함수가 무엇을 대표하는지 일반화하도록",
    "알고리즘적사고": "실행 순서를 단계별로 추적하도록",
    "통합":          "코드 전체의 목적과 흐름을 종합적으로 파악하도록",
}


_NUDGE_CONTEXT = {
    "wrong_answer": (
        "학생이 방금 문제를 틀렸습니다. "
        "정답을 직접 알려주지 말고, 코드의 어느 부분을 다시 살펴봐야 할지 "
        "가볍게 유도하는 질문을 먼저 건네세요."
    ),
    "inactivity": (
        "학생이 한동안 질문하지 않고 있습니다. "
        "학생이 막혀 있을 수 있으니 먼저 말을 걸어 "
        "현재 코드나 문제와 관련된 생각거리를 제시하는 질문을 건네세요."
    ),
}


def build_chat_system(
    code_context: str = None,
    current_problem: str = None,
    weak_ct: str = None,
) -> str:
    system = SYSTEM_CHAT
    if weak_ct:
        guide = _CT_GUIDANCE.get(weak_ct, f"'{weak_ct}' 역량을 높이도록")
        system += (
            f"\n\n[이 학생의 약점 — 집중 유도]\n"
            f"직전 세션에서 '{weak_ct}' 역량이 가장 약했다.\n"
            f"이번 대화에서는 학생이 {guide} 유도하는 질문을 우선적으로 사용하라."
        )
    if code_context:
        system += f"\n\n[현재 학습 중인 코드]\n{code_context}"
    if current_problem:
        system += f"\n\n[현재 풀고 있는 문제]\n{current_problem}"
    return system


def build_nudge_system(
    code_context: str = None,
    current_problem: str = None,
    reason: str = "inactivity",
    weak_ct: str = None,
) -> str:
    """챗봇 선제 유도용 시스템 프롬프트. reason에 따라 상황 힌트를 추가한다."""
    system = build_chat_system(code_context, current_problem, weak_ct)
    hint = _NUDGE_CONTEXT.get(reason, _NUDGE_CONTEXT["inactivity"])
    system += f"\n\n[지금 해야 할 일]\n{hint}"
    return system


def stream_chatbot(messages: list):
    """챗봇 SSE 스트리밍. Lock 미적용 — 스트리밍 중 Lock 점유로 다른 호출 차단 방지."""
    return _client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=4096,
        temperature=TEMP_CREATIVE,
        stream=True,
    )
