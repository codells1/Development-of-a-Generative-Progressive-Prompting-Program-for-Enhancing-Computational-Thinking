"""
llm.py — 모든 LLM 호출을 역할별로 분리·관리하는 중앙 모듈
"""

import re
import threading
from openai import OpenAI

BASE_URL = "http://localhost:1234/v1"
MODEL    = "local-model"

TEMP_CREATIVE = 0.6   # 챗봇, 코드/문제 생성 (객관식 형식 안정 위해 0.7→0.6)
TEMP_PRECISE  = 0.2   # CT 분석, 프롬프트 평가

_client   = OpenAI(base_url=BASE_URL, api_key="lm-studio")
_llm_lock = threading.Lock()


# ── System Prompts ──────────────────────────────────────────────────

SYSTEM_CHAT = (
    "당신은 컴퓨팅 사고력(CT) 학습을 돕는 소크라테스식 교육 AI다.\n"
    "학생이 스스로 답을 발견하도록 유도 질문으로만 대화한다.\n\n"
    "절대 금지:\n"
    "- 정답, 정답 코드, 정답 값을 직접 알려주지 않는다.\n"
    "- '정답은 ~입니다', 'A번이 맞습니다' 같은 직접 답변 금지.\n\n"
    "반드시 지킬 사항:\n"
    "- 모든 답변은 질문으로 끝낸다. 예: '그렇다면 x가 1일 때 어떻게 될까요?'\n"
    "- 코드의 어느 부분을 보면 힌트가 되는지 방향만 제시한다.\n"
    "- 한국어로 답변한다. 중학생도 이해할 수 있는 쉬운 말을 쓴다.\n"
    "- 학생 질문이 모호하면 어느 부분을 묻는지 되묻는다."
)

SYSTEM_CODE = (
    "You are a coding tutor for beginners. "
    "Return ONLY a runnable Python code block. "
    "No comments, no explanation, no markdown fences."
)

SYSTEM_PROBLEM = (
    "You are a coding tutor. Create exactly ONE multiple-choice question in Korean "
    "based on the given Python code.\n"
    "Output ONLY valid JSON — no markdown fences, no other text:\n"
    '{"question": "문제 본문", '
    '"options": ["A. 보기1", "B. 보기2", "C. 보기3", "D. 보기4"], '
    '"answer": "B", '
    '"explanation": "정답 해설 1~2문장", '
    '"ct_skill": "CT요소명", '
    '"difficulty": "난이도명"}\n'
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
    "Evaluate the QUALITY of the student's prompts/questions based on their chat log.\n\n"
    "Prompting Rubric (score each element 1~5):\n"
    "- 명확성(1~5): 질문이 명확하고 이해하기 쉬운가\n"
    "- 구체성(1~5): 코드·문제와 연관된 구체적 질문인가\n"
    "- 맥락제공(1~5): 학습 내용과 관련된 배경·맥락을 충분히 포함했는가\n"
    "- 관련성(1~5): 현재 학습 주제와 관련된 질문인가\n"
    "- 자기주도성(1~5): 스스로 생각하고 탐구하려는 의지가 보이는가\n"
    "- 발전성(1~5): 이전 질문보다 더 깊이 발전시켜 나갔는가\n\n"
    "If no student chat messages exist, score all elements 1.\n\n"
    'Respond ONLY with valid JSON (no markdown, no extra text):\n'
    '{"명확성": 점수, "구체성": 점수, "맥락제공": 점수, "관련성": 점수, '
    '"자기주도성": 점수, "발전성": 점수, "feedback": "한국어 피드백 2~3문장"}'
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
    if not content:
        reasoning = getattr(choice.message, "reasoning_content", None) or ""
        content = _extract_from_reasoning(reasoning)
    return content


# ── 생성 계열 (TEMP_CREATIVE, history 없음) ────────────────────────

def call_code_gen(topic: str, ctx: str = "") -> str:
    ctx_block = f"\n\n[참고 예제]\n{ctx}" if ctx else ""
    user_content = (
        f"/no_think [주제: {topic}]{ctx_block}\n\n"
        f"'{topic}'을 보여주는 간단한 예제 코드를 작성해줘."
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_CODE},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
    )


def call_code_gen_fusion(ctx: str = "") -> str:
    ctx_block = f"\n\n[참고 예제]\n{ctx}" if ctx else ""
    user_content = (
        f"/no_think [주제: 융합]{ctx_block}\n\n"
        "여러 프로그래밍 개념(변수·조건, 반복·리스트, 함수, 알고리즘)을 "
        "2개 이상 결합한 종합 예제 코드를 작성해줘."
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_CODE},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
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
    ctx_block = f"\n\n[참고 템플릿]\n{templates}" if templates else ""
    prev_text = (
        "\n\n[이미 출제된 문제 - 중복 금지]\n"
        + "\n".join(f"- {p}" for p in previous_problems)
    ) if previous_problems else ""
    user_content = (
        f"/no_think [CT 요소: {ct_skill}]\n"
        f"[난이도: {difficulty}] (총 5문제 중 {problem_index + 1}번째)"
        f"{ctx_block}{prev_text}\n\n"
        f"[파이썬 코드]\n{code}"
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


def stream_chatbot(messages: list):
    """챗봇 SSE 스트리밍. Lock 미적용 — 스트리밍 중 Lock 점유로 다른 호출 차단 방지."""
    return _client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=4096,
        temperature=TEMP_CREATIVE,
        stream=True,
    )
