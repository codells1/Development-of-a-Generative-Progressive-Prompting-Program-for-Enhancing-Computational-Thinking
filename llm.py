"""
llm.py — 모든 LLM 호출을 역할별로 분리·관리하는 중앙 모듈
"""

import os
import re
import threading
from openai import OpenAI

_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(filename: str) -> str:
    """prompts/ 폴더의 마크다운 파일을 읽어 문자열로 돌려준다. 없으면 빈 문자열."""
    path = os.path.join(_PROMPTS_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        print(f"프롬프트 파일 로드 실패({filename}): {e}")
        return ""

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
    "말투·형식 (반드시 일관 유지):\n"
    "- 항상 정중한 존댓말(해요체)로 답한다. 문장 끝은 ~요 / ~까요 / ~네요 형태로 맺는다. "
    "반말(~까?, ~같아?, ~거야 등)은 절대 쓰지 않는다.\n"
    "- 답변 전체를 큰따옴표(\")나 작은따옴표(')로 감싸지 않는다. "
    "코드의 변수·함수명 같은 짧은 식별자를 가리킬 때를 빼고는 따옴표를 쓰지 말고, 문장만 그대로 출력한다.\n"
    "- 중학생도 이해할 수 있는 쉬운 말로, 2~3문장 이내로 짧게.\n\n"
    "절대 금지:\n"
    "- 정답, 정답 코드, 정답 값을 직접 알려주지 않는다.\n"
    "- 정답은 ~입니다 / A번이 맞습니다 같은 직접 답변 금지.\n\n"
    "반드시 지킬 사항:\n"
    "- 모든 답변은 질문으로 끝낸다 (예: 그렇다면 x가 1일 때 어떻게 될까요?).\n"
    "- 코드의 어느 부분을 보면 힌트가 되는지 방향만 제시한다.\n"
    "- 학생 질문이 모호하면 어느 부분을 묻는지 되묻는다."
)

SYSTEM_CODE = (
    "너는 중·고등학생의 '코드 읽기' 학습용 파이썬 예제를 만드는 출제자다.\n"
    "규칙:\n"
    "1. 외부 입력 없이 그대로 실행되는 단일 완결 프로그램 1개. 데이터는 코드 안에 고정, input() 금지.\n"
    "2. 길이 최대 20줄(권장 12~18줄), 함수 1~2개. 20줄을 절대 넘기지 마라. "
    "분해·통합 문항을 위해 함수 2개를 권장한다. 변수·함수 이름은 의미가 드러나게.\n"
    "3. 권장 형태: 값을 계산하는 함수 1개(반복+조건 포함) + 그 결과를 쓰는 함수 1개(출력/판정).\n"
    "4. 코드에는 (a)함수 또는 처리 단계 2개 이상, (b)반복문, (c)함수로 세부를 감춘 부분, "
    "(d)조건 분기, (e)부분이 합쳐져 하나의 목적을 이루는 구조가 모두 있어야 한다.\n"
    "5. 아래 참고 예시가 있다면 구조·스타일만 본뜨고 그대로 복사하지 마라.\n"
    "6. 출력은 파이썬 코드 블록 하나만. 설명 문장·JSON 금지."
)

SYSTEM_PROBLEM = (
    "아래 파이썬 코드를 읽고 코드의 제목·한 줄 요약과 4지선다 MCQ 5문항을 한 번에 만든다.\n"
    "출력은 지정한 JSON 객체 1개만(코드블록 기호·설명 문장 금지).\n\n"
    "규칙:\n"
    "1. title: 코드 전체를 가리키는 짧은 한국어 제목.\n"
    "2. summary: 이 코드가 무엇을 하는지 한 줄로 요약한 한국어 문장.\n"
    "3. 5문항의 ct_skill은 순서대로 분해, 패턴인식, 추상화, 알고리즘적사고, 통합. 각 정확히 1문항.\n"
    "3-1. 유형마다 '묻는 각도'를 아래로 못박아 서로 겹치지 않게 한다. "
    "특히 '이 함수의 역할/동작이 무엇인가' 같은 문항을 여러 유형에 중복 출제하지 마라(가장 흔한 중복 실수다):\n"
    "   · 분해: 코드를 입력·처리·출력 등으로 나누면 몇 단계인지, 또는 어느 부분(몇 번째 줄·어느 함수)이 무슨 단계인지 — 구조 분할만 묻는다.\n"
    "   · 패턴인식: 반복에서 값이 변하는 규칙이나 n번째 출력값 등 반복 패턴을 묻는다.\n"
    "   · 추상화: 특정 변수·이름이 뜻하는 개념·의미가 무엇인지 묻는다(구체적 값·자료형 말고). 함수 전체의 역할은 묻지 마라.\n"
    "   · 알고리즘적사고: 코드를 단계별로 실행했을 때의 최종 변수값·출력 등 실행 흐름 추적을 묻는다.\n"
    "   · 통합: 코드 전체가 푸는 문제가 무엇인지, 또는 어떤 상황에 쓰는지(사용 맥락)·한 문장 요약을 묻는다.\n"
    "   같은 코드 부분(예: 같은 함수)을 두 문항 이상에서 똑같은 각도로 묻지 말고, 각 유형은 위 각도만 사용한다.\n"
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
    "input()·파일·네트워크·무한루프 금지.\n"
    "   - answer_type이 'conceptual'이면: 빈 문자열 \"\".\n"
    "   - computational 문항의 보기 값(라벨 뒤 부분)에는 단위·접미사(번, 개, 원, 명, 회 등)나 "
    "따옴표를 붙이지 말고, verification_snippet의 print 출력과 글자 그대로 정확히 같게 만든다. "
    "예: 스니펫이 2를 출력하면 보기는 'B. 2번'이 아니라 'B. 2'.\n"
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

# Stage 2 구조화 출력 강제용 JSON Schema (code_reading_generation.md §4).
# bare json_object가 아니라 json_schema로 넘겨 필드·타입·배열 길이·완결성까지 디코더가 강제한다.
# (ct_skill 값은 enum으로 제한하되, 순서·구성 분해→통합 각 1개 보정은 api._reconcile_skills가 담당.)
PROBLEM_SET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title":      {"type": "string"},
        "summary":    {"type": "string"},
        "difficulty": {"type": "string"},
        "code":       {"type": "string"},
        "questions": {
            "type": "array",
            "minItems": 5,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ct_skill":             {"type": "string",
                                             "enum": ["분해", "패턴인식", "추상화", "알고리즘적사고", "통합"]},
                    "question":             {"type": "string"},
                    "options":              {"type": "array", "minItems": 4, "maxItems": 4,
                                             "items": {"type": "string"},
                                             "description": "보기 4개('A. ...' 형식). computational 문항은 "
                                             "각 보기의 값 부분(라벨 뒤)에 단위·접미사(번/개/원/명/회 등)나 따옴표를 "
                                             "붙이지 말고 verification_snippet의 print 출력과 글자 그대로 같게 한다 "
                                             "(예: 2를 출력하면 'A. 2', 'A. 2번' 금지)."},
                    "answer":               {"type": "string", "enum": ["A", "B", "C", "D"]},
                    "answer_type":          {"type": "string",
                                             "enum": ["computational", "conceptual"]},
                    "verification_snippet": {"type": "string"},
                    "explanation":          {"type": "string"},
                    "focus_points":         {"type": "array", "minItems": 1, "maxItems": 3,
                                             "items": {"type": "string"}},
                },
                "required": ["ct_skill", "question", "options", "answer", "answer_type",
                             "verification_snippet", "explanation", "focus_points"],
            },
        },
    },
    "required": ["title", "summary", "difficulty", "code", "questions"],
}

PROBLEM_SET_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "problem_set", "strict": True, "schema": PROBLEM_SET_SCHEMA},
}

# 단일 문항 재생성용 스키마 — 세트의 문항 item 스키마를 그대로 재사용해 두 경로가
# 절대 어긋나지 않게 한다. (ct_skill·question·options 4개·answer·answer_type·
# verification_snippet·explanation·focus_points 전부 required, additionalProperties False.)
SINGLE_PROBLEM_SCHEMA = PROBLEM_SET_SCHEMA["properties"]["questions"]["items"]

SINGLE_PROBLEM_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "single_problem", "strict": True, "schema": SINGLE_PROBLEM_SCHEMA},
}


# CT 평가 루브릭은 prompts/ct_evaluation_rubric.md를 단일 출처로 삼는다.
# (채점 지표·기준·출력 JSON 형식 모두 그 파일에서 정의. 여기선 분석 지시만 덧붙인다.)
_CT_RUBRIC_MD = _load_prompt("ct_evaluation_rubric.md")

SYSTEM_CT_ANALYSIS = (
    "당신은 교육 평가 AI다.\n"
    "오직 '학생의 발화'만 근거로 학생의 컴퓨팅 사고력(CT)을 평정한다.\n"
    "대화 로그에서 '[학생·채점대상]' 줄만 채점 근거로 삼아라. "
    "'[AI·맥락(채점제외)]' 줄은 어떤 힌트·질문에 학생이 어떻게 반응했는지 맥락을 파악하는 "
    "데만 참조하고, 그 문장 자체로는 절대 점수를 매기지 마라.\n"
    "퀴즈 정답 여부로 점수를 매기지 말고, 학생이 '어떻게 질문·진술했는가'에 집중한다.\n\n"
    "아래 루브릭으로 각 지표를 채점하라.\n"
    "----- CT 평가 루브릭 -----\n"
    f"{_CT_RUBRIC_MD}\n"
    "----- 루브릭 끝 -----\n\n"
    "루브릭의 '출력 형식' 절에 정의된 JSON 객체 하나만 출력하라. "
    "마크다운 코드펜스·설명 문장을 덧붙이지 마라."
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


# ── Think 토큰 제거 (Qwen3 추론 모드가 <think>...</think>를 본문에 섞어 출력) ──

_THINK_OPEN  = "<think>"
_THINK_CLOSE = "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """완성된 문자열에서 <think>...</think> 블록을 통째로 제거한다."""
    # 닫는 태그 없이 think로만 끝나는 비정상 출력도 잘라낸다.
    text = _THINK_RE.sub("", text)
    open_idx = text.lower().find(_THINK_OPEN)
    if open_idx != -1 and _THINK_CLOSE not in text.lower()[open_idx:]:
        text = text[:open_idx]
    return text


class ThinkStreamFilter:
    """스트리밍 델타에서 <think>...</think> 구간을 실시간 제거한다.

    태그가 청크 경계에 걸쳐 쪼개져도 안전하도록, 부분 태그가 될 수 있는
    꼬리만 버퍼에 남기고 그 외 텍스트만 방출한다.
    """

    def __init__(self):
        self._buf = ""
        self._in_think = False

    def _safe_tail(self, tag: str) -> int:
        """버퍼 접미사가 tag 접두사와 일치하는 최대 길이(부분 태그 후보)를 반환."""
        for k in range(min(len(tag) - 1, len(self._buf)), 0, -1):
            if self._buf.endswith(tag[:k]):
                return k
        return 0

    def feed(self, text: str) -> str:
        """델타를 받아 화면에 내보낼(생각 구간이 제거된) 텍스트를 반환."""
        self._buf += text
        out = []
        while self._buf:
            if not self._in_think:
                idx = self._buf.find(_THINK_OPEN)
                if idx != -1:
                    out.append(self._buf[:idx])
                    self._buf = self._buf[idx + len(_THINK_OPEN):]
                    self._in_think = True
                    continue
                keep = self._safe_tail(_THINK_OPEN)
                out.append(self._buf[:len(self._buf) - keep] if keep else self._buf)
                self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                break
            else:
                idx = self._buf.find(_THINK_CLOSE)
                if idx != -1:
                    self._buf = self._buf[idx + len(_THINK_CLOSE):]
                    self._in_think = False
                    continue
                keep = self._safe_tail(_THINK_CLOSE)
                self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                break
        return "".join(out)

    def flush(self) -> str:
        """스트림 종료 시 남은 버퍼 처리. think 안이면 버리고, 밖이면 방출."""
        if self._in_think:
            self._buf = ""
            return ""
        out, self._buf = self._buf, ""
        return out


# ── Single Entry Point ─────────────────────────────────────────────

def _generate(messages: list, temperature: float, max_tokens: int = 4096,
              response_format: dict = None) -> str:
    """모든 비스트리밍 LLM 호출의 단일 진입점. Lock으로 동시 호출을 직렬화한다."""
    kwargs = dict(
        model=MODEL,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    if response_format is not None:
        kwargs["response_format"] = response_format
    with _llm_lock:
        resp = _client.chat.completions.create(**kwargs)
    choice = resp.choices[0]
    content = strip_think(choice.message.content or "").strip()
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
    # 코드 생성은 추론 없이도 충분하고 가장 느린 단계라 think를 끈다 (Qwen3 /no_think).
    user_content = f"/no_think 난이도: {difficulty} / 주제 힌트: {topic}"
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
        response_format=PROBLEM_SET_RESPONSE_FORMAT,
    )


SYSTEM_SINGLE_PROBLEM = (
    "주어진 파이썬 코드에 대해 지정된 CT 요소 한 가지에 맞는 4지선다 MCQ 1문항을 만든다.\n"
    "출력은 JSON 객체 1개만 (코드블록 기호·설명 문장 금지).\n\n"
    "규칙:\n"
    "1. 보기 정확히 4개(A/B/C/D), 정답 1개. 오답도 그럴듯하게.\n"
    "2. answer_type 분류:\n"
    "   - 'computational': 코드를 실행하면 값이 하나로 정해지는 문항. verification_snippet은 필요한 함수 정의를 모두 그 안에 포함한 자족(self-contained) 실행 코드로, 정답 값 하나만 print 한다. 위 코드의 함수를 쓰려면 그 정의를 스니펫 안에 다시 적어라. input()·파일·네트워크 금지.\n"
    "   - 'conceptual': 코드의 의미·구조·역할·목적을 묻는 문항. verification_snippet은 \"\".\n"
    "3. computational이면 보기 값(라벨 뒤)에 단위·접미사(번/개/원/명/회 등)나 따옴표를 붙이지 말고, verification_snippet의 print 출력과 글자 그대로 정확히 일치시킨다 (예: 5를 출력하면 'A. 5', 'A. 5회' 금지).\n"
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
        response_format=SINGLE_PROBLEM_RESPONSE_FORMAT,
    )


# ── 분석 계열 (TEMP_PRECISE, history 미주입) ────────────────────────

def call_log_analysis(log_text: str) -> str:
    """CT 측정. 대화 로그를 분석 대상 자료로만 전달 (history 미주입)."""
    return _generate(
        [{"role": "system", "content": SYSTEM_CT_ANALYSIS},
         {"role": "user",   "content": f"/no_think [분석할 학습 기록]\n{log_text}"}],
        TEMP_PRECISE,
        max_tokens=800,   # 상중하 점수 + 서술형 피드백(3~5문장)까지 담을 여유
    )


# ── 챗봇 (스트리밍, Lock 미적용) ────────────────────────────────────

def build_chat_system(code_context: str = None, current_problem: str = None,
                      ct_skill: str = None, focus_points=None) -> str:
    system = SYSTEM_CHAT
    if code_context:
        system += f"\n\n[현재 학습 중인 코드]\n{code_context}"
    if current_problem:
        system += f"\n\n[현재 풀고 있는 문제]\n{current_problem}"
    if ct_skill:
        system += (
            f"\n\n[이 문항이 기르려는 컴퓨팅 사고력 요소] {ct_skill}\n"
            "유도 질문이 이 요소를 자극하는 방향이 되게 하라."
        )
    if focus_points:
        fp = focus_points if isinstance(focus_points, (list, tuple)) else [focus_points]
        fp_text = "\n".join(f"- {p}" for p in fp)
        system += (
            "\n\n[유도용 핵심 포인트 — 너만 참고하는 비공개 단서]\n"
            f"{fp_text}\n"
            "이 단서는 학생에게 그대로 알려주지 말고, 학생이 스스로 이 방향을 떠올리도록 "
            "유도 질문의 소재로만 써라. 정답·정답 값·정답 라벨, 맞고 틀림 판정은 여전히 절대 말하지 마라."
        )
    return system


# ct_skill별 '생각해볼 거리' 방향 + 예시. 예시는 존댓말·따옴표 없이 둔다(모델이 말투·따옴표를 그대로 베끼지 않도록).
_HINT_DIRECTION_BY_SKILL = {
    "분해":         "코드를 어떤 부분(함수·처리 단계)으로 나눠볼지 떠올리게 하라. "
                    "예: 이 코드를 어떤 부분들로 나눠볼 수 있을까요?",
    "패턴인식":     "반복되며 규칙적으로 일어나는 동작에 주목하게 하라. "
                    "예: 여기서 반복되는 동작에는 어떤 규칙이 있을까요?",
    "추상화":       "함수가 어떤 구체적 과정을 감추고 무엇을 대표하는지 떠올리게 하라. "
                    "예: 이 함수는 복잡한 과정을 어떤 한 가지 일로 묶어 감추고 있을까요?",
    "알고리즘적사고": "입력을 따라가며 변수·실행 흐름이 어떻게 바뀌는지 추적하게 하라. "
                    "예: 이 입력을 넣으면 변수가 어떻게 바뀌는지 한 줄씩 따라가 볼까요?",
    "통합":         "부분들이 합쳐져 전체가 무엇을 이루는지 생각하게 하라. "
                    "예: 각 함수가 합쳐져 무엇을 만드는 것 같나요?",
}


def build_trigger_user_message(trigger_type: str, ct_skill: str = None) -> str:
    """챗봇이 먼저 말하도록 만드는 유사-유저 메시지. 학생 발화로 기록하지 않는다."""
    if trigger_type == "hint":
        msg = (
            "[학생 행동: 힌트 버튼 클릭]\n"
            "학생이 현재 문제에 대한 힌트를 요청했다.\n"
        )
        direction = _HINT_DIRECTION_BY_SKILL.get(ct_skill or "")
        if direction:
            msg += f"이 문항의 컴퓨팅 사고력 요소는 '{ct_skill}'이다. {direction}\n"
        msg += (
            "정답·정답 라벨을 절대 말하지 말고, 위 방향에 맞는 생각해볼 거리를 "
            "소크라테스식 유도 질문 한 개로만 던져라. 2~3문장 이내. "
            "정중한 존댓말(해요체, ~까요?/~요)로 쓰고, 답변을 따옴표로 감싸지 마라. "
            "예시 문장을 그대로 베끼지 말고 지금 코드·문제에 맞게 새로 만들어라."
        )
        return msg
    if trigger_type == "explain":
        return (
            "[학생 행동: 힌트 없이 정답을 맞춤]\n"
            "학생이 이 문제를 스스로 풀었다.\n"
            "학생이 자기 풀이 과정을 스스로 말로 풀어내도록 요청하는 메시지를 작성하라 "
            "(예: 이 문제를 어떻게 풀었는지 설명해 줄 수 있나요?). 1~2문장으로 짧게. "
            "정중한 존댓말(해요체, ~까요?/~요)로 쓰고, 답변을 따옴표로 감싸지 마라."
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
