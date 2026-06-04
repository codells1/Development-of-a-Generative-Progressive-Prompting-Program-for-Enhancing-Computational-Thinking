"""
llm.py — 모든 LLM 호출을 역할별로 분리·관리하는 중앙 모듈
"""

import re
import threading
from openai import OpenAI

BASE_URL = "http://localhost:1234/v1"
MODEL    = "local-model"

TEMP_CREATIVE = 0.6   # 챗봇, 코드/문제 생성 (객관식 형식 안정 위해 0.7→0.6)
TEMP_PRECISE  = 0.2   # CT 분석

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
    "너는 중·고등학생의 '코드 읽기' 학습용 파이썬 예제를 만드는 출제자다.\n"
    "규칙:\n"
    "1. 외부 입력 없이 그대로 실행되는 단일 완결 프로그램 1개. 데이터는 코드 안에 고정, input() 금지.\n"
    "2. 길이 약 30~50줄, 함수 3~5개. 변수·함수 이름은 의미가 드러나게.\n"
    "3. 코드에는 (a)구분되는 함수 2개 이상, (b)반복문, (c)함수로 세부를 감춘 부분, "
    "(d)조건 분기, (e)부분이 합쳐져 하나의 목적을 이루는 구조가 모두 있어야 한다.\n"
    "4. 아래 참고 예시가 있다면 구조·스타일만 본뜨고 그대로 복사하지 마라.\n"
    "5. 출력은 파이썬 코드 블록 하나만. 설명 문장 금지."
)

SYSTEM_PROBLEM = (
    "아래 파이썬 코드를 읽고 코드의 제목·한 줄 요약과 4지선다 MCQ 5문항을 한 번에 만든다.\n"
    "출력은 지정한 JSON 객체 1개만(코드블록 기호·설명 문장 금지).\n\n"
    "규칙:\n"
    "1. title: 코드 전체를 가리키는 짧은 한국어 제목.\n"
    "2. summary: 이 코드가 무엇을 하는지 한 줄로 요약한 한국어 문장.\n"
    "3. 5문항의 ct_skill은 순서대로 분해, 패턴인식, 추상화, 알고리즘적사고, 통합. 각 정확히 1문항.\n"
    "4. 각 문항은 주어진 코드를 읽으면 풀 수 있어야 한다.\n"
    "5. 각 문항 보기는 정확히 4개(A/B/C/D), 정답은 그 중 1개. 오답 보기도 그럴듯하게.\n"
    "6. 정답 라벨이 한 자리에 쏠리지 않게 5문항에 걸쳐 다양하게 분포시킨다.\n"
    "7. 각 문항에 answer_type을 분류해 넣는다:\n"
    "   - 'computational': 코드를 실행하면 값이 하나로 정해지는 문항(출력값, n번 반복 후 변수값, 함수 반환값 등).\n"
    "   - 'conceptual': 코드의 의미·구조·역할·목적을 묻는 문항.\n"
    "   강제는 아니지만 보통 알고리즘적사고(가끔 패턴인식)가 computational, 분해·추상화·통합은 conceptual이 자연스럽다.\n"
    "8. 각 문항의 verification_snippet:\n"
    "   - answer_type이 'computational'이면: 정답 값을 구하는 자족(self-contained) 파이썬 코드를 적는다. "
    "필요한 함수 정의를 모두 그 안에 포함하고, 정답 값 하나만 print 한다. "
    "input()·파일·네트워크·무한루프 금지. "
    "출력 형식을 보기의 값 부분과 정확히 맞춘다 (예: 보기 'B. 22000' → print 결과는 '22000').\n"
    "   - answer_type이 'conceptual'이면: 빈 문자열 \"\".\n"
    "9. 각 문항에 focus_points(1~3개의 한국어 문자열 배열)를 넣는다. "
    "정답을 그대로 적지 말고 학생이 풀이를 떠올리는 '생각의 단서'(예: 살펴봐야 할 코드 영역, 추적할 변수, 호출 흐름)로 적는다.\n\n"
    'JSON 형식:\n'
    '{"title": "프로그램 제목",\n'
    ' "summary": "이 프로그램이 하는 일 한 줄",\n'
    ' "questions": [\n'
    '  {"ct_skill": "분해", "question": "...", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], "answer": "B", "answer_type": "conceptual", "verification_snippet": "", "explanation": "정답 해설 1~2문장", "focus_points": ["살펴볼 단서 1", "살펴볼 단서 2"]},\n'
    '  {"ct_skill": "패턴인식", ...},\n'
    '  {"ct_skill": "추상화", ...},\n'
    '  {"ct_skill": "알고리즘적사고", "...", "answer_type": "computational", "verification_snippet": "def calc(...):\\n    ...\\nprint(calc(...))", ...},\n'
    '  {"ct_skill": "통합", ...}\n'
    ']}'
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

def call_code_gen(topic: str = "", ctx: str = "", difficulty: str = "") -> str:
    """Stage 1 — 단일 완결 파이썬 프로그램 생성 (code_reading_generation.md §2)."""
    system = SYSTEM_CODE
    if ctx:
        system += f"\n\n[참고 예시]\n{ctx}"
    user_content = f"난이도: {difficulty} / 주제 힌트: {topic}"
    return _generate(
        [{"role": "system", "content": system},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
    )


def call_problem_gen(code: str, templates: str = "", difficulty: str = "") -> str:
    """Stage 2 — 한 번 호출로 MCQ 5문항 일괄 생성 (code_reading_generation.md §3)."""
    system = SYSTEM_PROBLEM
    if templates:
        system += f"\n\n[유형별 출제 가이드]\n{templates}"
    user_content = f"난이도: {difficulty}\n[파이썬 코드]\n{code}"
    return _generate(
        [{"role": "system", "content": system},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
        max_tokens=4096,
    )


SYSTEM_SINGLE_PROBLEM = (
    "주어진 파이썬 코드에 대해 지정된 CT 요소 한 가지에 맞는 4지선다 MCQ 1문항을 만든다.\n"
    "출력은 JSON 객체 1개만 (코드블록 기호·설명 문장 금지).\n\n"
    "규칙:\n"
    "1. 보기 정확히 4개(A/B/C/D), 정답 1개. 오답도 그럴듯하게.\n"
    "2. answer_type 분류:\n"
    "   - 'computational': 코드를 실행하면 값이 하나로 정해지는 문항. verification_snippet은 정답 값 하나만 print하는 자족 실행 코드, input()·파일·네트워크 금지.\n"
    "   - 'conceptual': 코드의 의미·구조·역할·목적을 묻는 문항. verification_snippet은 \"\".\n"
    "3. computational이면 보기의 값 형식과 verification_snippet의 print 출력을 정확히 일치시킨다.\n"
    "4. focus_points: 1~3개의 한국어 문자열 배열. 정답을 그대로 적지 말고 학생이 풀이를 떠올리는 '생각의 단서'로 적는다.\n\n"
    'JSON 형식:\n'
    '{"ct_skill": "지정된 CT 요소", "question": "...", "options": ["A. ...", "B. ...", "C. ...", "D. ..."], '
    '"answer": "B", "answer_type": "computational", "verification_snippet": "...", "explanation": "정답 해설 1~2문장", "focus_points": ["...", "..."]}'
)


def call_single_problem_gen(code: str, ct_skill: str, templates: str = "") -> str:
    """단일 문항 재생성 (검증 실패한 computational 문항용)."""
    system = SYSTEM_SINGLE_PROBLEM
    if templates:
        system += f"\n\n[유형별 출제 가이드]\n{templates}"
    user_content = f"CT 요소: {ct_skill}\n[파이썬 코드]\n{code}"
    return _generate(
        [{"role": "system", "content": system},
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


# ── 챗봇 (스트리밍, Lock 미적용) ────────────────────────────────────

def build_chat_system(code_context: str = None, current_problem: str = None) -> str:
    system = SYSTEM_CHAT
    if code_context:
        system += f"\n\n[현재 학습 중인 코드]\n{code_context}"
    if current_problem:
        system += f"\n\n[현재 풀고 있는 문제]\n{current_problem}"
    return system


def build_trigger_user_message(trigger_type: str) -> str:
    """챗봇이 먼저 말하도록 만드는 유사-유저 메시지. 학생 발화로 기록하지 않는다."""
    if trigger_type == "hint":
        return (
            "[학생 행동: 힌트 버튼 클릭]\n"
            "학생이 현재 문제에 대한 힌트를 요청했다.\n"
            "정답·정답 라벨을 절대 말하지 말고, 학생이 스스로 답을 찾도록 돕는 "
            "소크라테스식 유도 질문 한 개만 한국어로 던져라. 2~3문장 이내."
        )
    if trigger_type == "explain":
        return (
            "[학생 행동: 힌트 없이 정답을 맞춤]\n"
            "학생이 이 문제를 스스로 풀었다.\n"
            "'이 문제를 어떻게 풀었는지 설명해 줄 수 있어?'처럼 학생이 자기 풀이 과정을 "
            "스스로 말로 풀어내도록 요청하는 한국어 메시지를 작성하라. 1~2문장으로 짧게."
        )
    raise ValueError(f"unknown trigger_type: {trigger_type!r}")


def stream_chatbot(messages: list):
    """챗봇 SSE 스트리밍. Lock 미적용 — 스트리밍 중 Lock 점유로 다른 호출 차단 방지."""
    return _client.chat.completions.create(
        model=MODEL,
        messages=messages,
        max_tokens=4096,
        temperature=TEMP_CREATIVE,
        stream=True,
    )
