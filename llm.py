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

# LM Studio 엔드포인트·모델. 환경변수로 덮어쓰면 코드 수정 없이 모델 교체 가능.
#   LMSTUDIO_BASE_URL / LMSTUDIO_MODEL
# 모델 ID는 LM Studio 모델 페이지 경로 형식(publisher/model-key)을 그대로 쓴다.
# (Q6_K 등 양자화는 LM Studio에서 로드할 때 고르는 것이라 API 모델 ID에는 안 들어간다.)
BASE_URL = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
MODEL    = os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3.5-9b")

TEMP_CREATIVE = 0.6   # 챗봇, 코드/문제 생성 (객관식 형식 안정 위해 0.7→0.6)
TEMP_PRECISE  = 0.2   # CT 분석

_client   = OpenAI(base_url=BASE_URL, api_key="lm-studio")
_llm_lock = threading.Lock()


# ── 언어 가드 (중국어·기타 외국어 혼입 방지) ────────────────────────
# 모든 호출의 시스템 메시지 끝에 자동으로 덧붙여, 어떤 단계든 한국어로만 출력하게 한다.
_LANG_GUARD = (
    "\n\n[언어 규칙] 설명·해설·질문·피드백 등 자연어 문장은 한국어로만 쓰고, "
    "중국어(한자)·일본어 등 다른 언어를 섞지 않는다. "
    "단, 파이썬 코드 자체(변수·함수 이름, 키워드 등 식별자)는 영문(ASCII) snake_case로 둔다 — "
    "식별자를 한글로 짓지 마라(주석·문자열 메시지는 한국어 가능)."
)


def _with_lang_guard(messages: list) -> list:
    """시스템 메시지 끝에 언어 가드를 붙인 새 메시지 리스트를 돌려준다."""
    if messages and messages[0].get("role") == "system":
        head = {**messages[0], "content": messages[0]["content"] + _LANG_GUARD}
        return [head, *messages[1:]]
    return [{"role": "system", "content": _LANG_GUARD.strip()}, *messages]


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
    "너는 '코드 읽기' 학습용 파이썬 예제를 만드는 출제자다.\n"
    "대상 수준: 중학교 3학년~고등학교 1학년. 이 수준에 맞춰 일관되게 만든다 "
    "(리스트·딕셔너리·문자열, for/while 반복, if 조건, 간단한 함수 정의·호출, 기본 산술까지만 사용. "
    "클래스·재귀·예외처리·컴프리헨션·람다·외부 라이브러리 등 고급 문법은 쓰지 않는다).\n"
    "규칙:\n"
    "1. 외부 입력 없이 그대로 실행되는 단일 완결 프로그램 1개. 데이터는 코드 안에 고정, input() 금지.\n"
    "2. 길이 최대 20줄(권장 12~18줄), 함수 1~2개. 20줄을 절대 넘기지 마라(빈 줄 포함). "
    "분해·통합 문항을 위해 함수 2개를 권장한다. "
    "★ 변수·함수 이름은 의미가 드러나는 영문 snake_case로 짓는다(예: total_price, grade_quiz). "
    "한글 이름(예: 계산_점수)·한글 변수는 쓰지 마라. 식별자는 영문, 주석·문자열 메시지는 한국어로 한다.\n"
    "3. 권장 형태: 값을 계산하는 함수 1개(반복+조건 포함) + 그 결과를 쓰는 함수 1개(출력/판정).\n"
    "4. 코드에는 (a)함수 또는 처리 단계 2개 이상, (b)반복문, (c)함수로 세부를 감춘 부분, "
    "(d)조건 분기, (e)부분이 합쳐져 하나의 목적을 이루는 구조가 모두 있어야 한다.\n"
    "5. 아래 참고 예시가 있다면 구조·스타일만 본뜨고 그대로 복사하지 마라.\n"
    "6. 출력은 파이썬 코드 블록 하나만. 설명 문장·JSON 금지."
)

SYSTEM_PROBLEM = (
    "아래 파이썬 코드를 읽고 코드의 제목·한 줄 요약과 4지선다 MCQ 5문항을 한 번에 만든다.\n"
    "출력은 지정한 JSON 객체 1개만(코드블록 기호·설명 문장 금지).\n"
    "문항 난이도는 중학교 3학년~고등학교 1학년 수준으로 통일한다 "
    "(주어진 코드만 읽으면 풀 수 있고, 고급 개념을 요구하지 않는다).\n\n"
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
    "   - 'computational': 코드를 실행하면 값이 하나로 정해지는 문항(출력값, n번 반복 후 변수값, 함수 반환값 등). 정답 보기는 그 값 자체.\n"
    "   - 'conceptual': 코드의 의미·구조·역할·목적을 묻는 문항. 정답 보기가 설명 문장이면 conceptual로 둔다(서술형 답을 computational로 분류하지 마라).\n"
    "   강제는 아니지만 보통 알고리즘적사고(가끔 패턴인식)가 computational, 분해·추상화·통합은 conceptual이 자연스럽다.\n"
    "8. 각 문항의 verification_snippet:\n"
    "   - answer_type이 'computational'이면: 정답 값을 구하는 자족(self-contained) 파이썬 코드를 적는다. "
    "필요한 함수 정의를 모두 그 안에 포함한다. ★ 마지막 줄은 반드시 print()로 정답 값 하나만 출력한다 "
    "(함수를 호출만 하고 print를 빠뜨리면 안 됨: 'calc(...)' ✕ → 'print(calc(...))' ○). "
    "그리고 그 print 출력값이 정답으로 표시한 보기(answer)의 값과 정확히 같아야 한다. "
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
        # difficulty·code는 echo만 시키고 파싱에 안 써서 토큰 낭비·JSON 잘림(truncation)의 원인이라 스키마에서 뺀다.
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
    "required": ["title", "summary", "questions"],
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

# ── 사고(thinking) 건너뛰기 — qwen3.5-9b 대응 ──────────────────────
# 이 모델은 LM Studio 템플릿이 어시스턴트 턴에 <think>를 자동으로 열어주는 탓에,
# 그냥 호출하면 사고과정(평문)을 한참 쏟다가 </think> 뒤에야 답을 낸다(느리고 누출).
# /no_think·enable_thinking=False 등 토글은 이 모델에 안 먹힌다.
# 해결: 어시스턴트 메시지를 </think>로 '프리필'해 사고 블록을 즉시 닫고 곧장 답하게 한다.
# (실측: 27~52초 → 0.5~5초, 사고 누출 없음. 스트리밍·JSON 스키마·코드생성 모두 정상.)
_SKIP_THINK_PREFILL = {"role": "assistant", "content": "</think>\n\n"}


def _skip_think(messages: list) -> list:
    """메시지 끝에 </think> 프리필을 붙여 모델이 사고를 건너뛰고 바로 답하게 한다."""
    return [*messages, dict(_SKIP_THINK_PREFILL)]


# ── 방어용 </think> 스트리퍼 ────────────────────────────────────────
# 프리필로 보통은 사고가 없지만, 혹시 모델이 사고 블록을 흘리면 안전망으로 잘라낸다.
# 이 모델의 사고는 여는 <think>가 프롬프트에 있어 본문엔 '…</think> 답변' 형태로만
# 새므로, </think>가 보이면 그 뒤(진짜 답변)만 남긴다. <think>…</think> 쌍도 함께 제거.
_THINK_CLOSE = "</think>"
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# 사고 블록이 새고 있다는 신호(이 단어들이 보이면 </think>가 올 때까지 방출을 보류).
_THINK_MARKERS = ("<think>", "thinking process", "**analyze")


def strip_think(text: str) -> str:
    """완성된 문자열에서 사고 블록을 제거한다. </think>가 있으면 그 뒤만 남긴다."""
    text = _THINK_RE.sub("", text)
    low = text.lower()
    idx = low.rfind(_THINK_CLOSE)
    if idx != -1:
        text = text[idx + len(_THINK_CLOSE):]
    return text.lstrip("\n")


class ThinkStreamFilter:
    """스트리밍 방어 필터. 프리필 덕에 보통은 그대로 통과시키지만,
    혹시 사고 블록이 새면 </think>가 나올 때까지 방출을 보류하고
    </think> 뒤(진짜 답변)부터 내보낸다."""

    def __init__(self):
        self._buf = ""
        self._passthrough = False   # 사고 판정이 끝나 통과 모드로 전환됐는지

    def feed(self, text: str) -> str:
        if self._passthrough:
            return text
        self._buf += text
        low = self._buf.lower()
        idx = low.find(_THINK_CLOSE)
        if idx != -1:
            # 사고 블록 끝 — 그 뒤만 방출하고 이후는 통과.
            rest = self._buf[idx + len(_THINK_CLOSE):]
            self._buf = ""
            self._passthrough = True
            return rest.lstrip("\n")
        # 아직 </think> 없음. 사고 마커가 안 보이고 버퍼가 충분히 쌓였으면 통과로 확정.
        if not any(m in low for m in _THINK_MARKERS) and len(self._buf) >= 24:
            out, self._buf = self._buf, ""
            self._passthrough = True
            return out
        return ""   # 사고처럼 보이면 방출 보류(계속 버퍼링)

    def flush(self) -> str:
        """스트림 종료 시 남은 버퍼 처리."""
        out, self._buf = self._buf, ""
        if not self._passthrough and any(m in out.lower() for m in _THINK_MARKERS):
            return ""   # </think> 없이 끝난 사고 블록은 버린다
        return out


# ── Single Entry Point ─────────────────────────────────────────────

def _generate(messages: list, temperature: float, max_tokens: int = 4096,
              response_format: dict = None) -> str:
    """모든 비스트리밍 LLM 호출의 단일 진입점. Lock으로 동시 호출을 직렬화한다."""
    kwargs = dict(
        model=MODEL,
        messages=_skip_think(_with_lang_guard(messages)),
        temperature=temperature,
        max_tokens=max_tokens,
        stream=False,
    )
    if response_format is not None:
        kwargs["response_format"] = response_format
    with _llm_lock:
        resp = _client.chat.completions.create(**kwargs)
    return strip_think(resp.choices[0].message.content or "").strip()


# ── 생성 계열 (TEMP_CREATIVE, history 없음) ────────────────────────

def call_code_gen(topic: str = "", ctx: str = "", difficulty: str = "") -> str:
    """Stage 1 — 단일 완결 파이썬 프로그램 생성 (code_reading_generation.md §2)."""
    system = SYSTEM_CODE
    if ctx:
        system += f"\n\n[참고 예시]\n{ctx}"
    # 사고는 _generate의 </think> 프리필로 일괄 차단된다(여기서 토큰 지시 불필요).
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
        max_tokens=6144,   # 5문항+스니펫 JSON이 잘리지 않도록 여유 (truncation 방지)
        response_format=PROBLEM_SET_RESPONSE_FORMAT,
    )


SYSTEM_SINGLE_PROBLEM = (
    "주어진 파이썬 코드에 대해 지정된 CT 요소 한 가지에 맞는 4지선다 MCQ 1문항을 만든다.\n"
    "출력은 JSON 객체 1개만 (코드블록 기호·설명 문장 금지).\n\n"
    "규칙:\n"
    "1. 보기 정확히 4개(A/B/C/D), 정답 1개. 오답도 그럴듯하게.\n"
    "2. answer_type 분류:\n"
    "   - 'computational': 정답 보기가 코드 실행으로 정해지는 값(숫자·문자열) 자체인 문항. verification_snippet은 필요한 함수 정의를 모두 그 안에 포함한 자족(self-contained) 실행 코드로, ★ 마지막 줄에서 반드시 print()로 정답 값 하나만 출력한다(함수 호출만 하고 print 빠뜨리지 마라: 'calc(...)' ✕ → 'print(calc(...))' ○). 그 출력값은 정답 보기(answer) 값과 정확히 같아야 한다. 위 코드의 함수를 쓰려면 그 정의를 스니펫 안에 다시 적어라. input()·파일·네트워크 금지.\n"
    "   - 'conceptual': 코드의 의미·구조·역할·목적을 묻거나 정답 보기가 설명 문장인 문항. verification_snippet은 \"\". (서술형 답을 computational로 분류하지 마라.)\n"
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
        max_tokens=2048,   # 단일 문항+스니펫이 잘리지 않도록 여유
        response_format=SINGLE_PROBLEM_RESPONSE_FORMAT,
    )


# ── 분석 계열 (TEMP_PRECISE, history 미주입) ────────────────────────

def call_log_analysis(log_text: str) -> str:
    """CT 측정. 대화 로그를 분석 대상 자료로만 전달 (history 미주입)."""
    return _generate(
        [{"role": "system", "content": SYSTEM_CT_ANALYSIS},
         {"role": "user",   "content": f"[분석할 학습 기록]\n{log_text}"}],
        TEMP_PRECISE,
        max_tokens=1500,   # 7요소 점수 + 평가 근거(발화 인용 3~6개) + 서술형 피드백까지 담을 여유
    )


# ── 힌트 후속 판단 (학생 생각이 정답 방향에 맞는지) ──────────────────

SYSTEM_HINT_FOLLOWUP = (
    "너는 컴퓨팅 사고력 학습을 돕는 소크라테스식 교육 AI다.\n"
    "학생이 힌트를 받고 자기 생각을 말했다. 그 생각이 '정답 방향'에 맞는지 판단한다.\n"
    "- 핵심 개념이 정답 방향과 일치하면(표현이 거칠어도 개념이 맞으면) on_track=true.\n"
    "- 틀렸거나 오해·혼동이 있거나, 아직 방향을 못 잡았으면 on_track=false.\n\n"
    "reply 작성 규칙:\n"
    "- on_track=true이면 reply는 빈 문자열(\"\"). 더 이상 설명·질문하지 않는다(대화 종료).\n"
    "- on_track=false이면, 정답을 직접 알려주지 말고 학생이 스스로 오해를 바로잡도록 돕는 "
    "유도 질문 한 개를 reply에 쓴다. 정중한 존댓말(해요체, ~까요?/~요), 2~3문장 이내, "
    "답변을 따옴표로 감싸지 마라.\n"
    "정답·정답 라벨·정답 값은 어떤 경우에도 말하지 마라.\n"
    "출력은 JSON 객체 하나만: {\"on_track\": true 또는 false, \"reply\": \"...\"}"
)

HINT_FOLLOWUP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "on_track": {"type": "boolean"},
        "reply":    {"type": "string"},
    },
    "required": ["on_track", "reply"],
}

HINT_FOLLOWUP_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "hint_followup", "strict": True, "schema": HINT_FOLLOWUP_SCHEMA},
}


def call_hint_followup(code: str, current_problem: str, answer: str, explanation: str,
                       focus_points, transcript: str) -> str:
    """힌트 후 학생 발화가 정답 방향에 맞는지 판단. {on_track, reply} JSON 문자열 반환.
    정답·해설·핵심 단서는 판단 기준으로만 쓰고 학생에게 노출하지 않는다."""
    system = SYSTEM_HINT_FOLLOWUP
    if current_problem:
        system += f"\n\n[현재 문제]\n{current_problem}"
    if code:
        system += f"\n\n[코드]\n{code}"
    secret = ""
    if answer:
        secret += f"정답 라벨: {answer}\n"
    if explanation:
        secret += f"해설: {explanation}\n"
    if focus_points:
        fp = focus_points if isinstance(focus_points, (list, tuple)) else [focus_points]
        secret += "핵심 단서: " + " / ".join(str(p) for p in fp)
    if secret:
        system += ("\n\n[비공개 판단 기준 — 학생에게 절대 노출 금지]\n" + secret)
    user = (
        "[학생과의 최근 대화]\n"
        f"{transcript}\n\n"
        "위 마지막 학생 발화의 생각이 정답 방향에 맞는지 판단해 JSON으로만 답하라."
    )
    return _generate(
        [{"role": "system", "content": system},
         {"role": "user",   "content": user}],
        TEMP_PRECISE,
        max_tokens=400,
        response_format=HINT_FOLLOWUP_RESPONSE_FORMAT,
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
        messages=_skip_think(_with_lang_guard(messages)),
        max_tokens=4096,
        temperature=TEMP_CREATIVE,
        stream=True,
    )
