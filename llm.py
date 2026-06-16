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


def _ensure_user_turn(messages: list) -> list:
    """모델 채팅 템플릿(Qwen 등)은 user 메시지가 하나도 없으면 렌더링을 거부한다
    (jinja: 'No user query found in messages'). </think> 프리필은 끝에 assistant 메시지를
    덧붙이므로, 앞에 user 발화가 비어 있으면 [system, assistant]만 남아 이 오류가 난다.
    트리거 불일치·빈 히스토리 등으로 user 발화가 빌 때를 위한 방어선."""
    if any(m.get("role") == "user" for m in messages):
        return messages
    return [*messages, {"role": "user", "content": "이 문제 힌트를 부탁해요."}]


# ── System Prompts ──────────────────────────────────────────────────

SYSTEM_CHAT = (
    "당신은 컴퓨팅 사고력(CT) 학습을 돕는 소크라테스식 교육 AI다.\n"
    "학생이 스스로 답을 발견하도록 유도 질문으로만 대화한다.\n\n"

    "[힌트 원리 — Block Model 상향식]\n"
    "모든 힌트는 아래 4단계 사다리를 '가장 낮은 단부터' 같은 순서로 오른다. "
    "유형이 달라도 사다리는 동일하고, 각 단의 '내용'만 유형에 맞게 달라진다.\n"
    "  Atom(원자)     : 가장 작은 단위(한 줄·한 요소)의 의미\n"
    "  Block(묶음)    : 함께 동작하는 묶음이 하는 일\n"
    "  Relation(관계) : 묶음과 묶음 사이의 연결·흐름\n"
    "  Macro(전체)    : 그 묶음들이 이루는 전체\n"
    "원칙: 학생이 막힌 가장 낮은 단(Atom)부터 시작한다. 그 단을 이해하면 한 단만 위로 올린다. "
    "단을 건너뛰지 않는다. 정답·정답 코드·출력값을 직접 노출하지 말고 학생이 스스로 도달하게 유도한다.\n\n"

    "[유형별 4단계 내용]\n"
    "현재 문항의 유형은 [이 문항이 기르려는 컴퓨팅 사고력 요소]로, 보는 코드 종류는 [이 문항이 보는 코드 종류]로 판별한다.\n"
    "· 문제분해 (의사코드 · 단계 순서)\n"
    "  Atom=의사코드 한 줄(단계)이 하는 일 / Block=이어지는 단계들이 이루는 묶음(준비·처리·마무리) / "
    "Relation=단계 간 순서·의존(앞 단계가 뒤 단계의 무엇을 준비하나) / Macro=전체 절차의 흐름\n"
    "· 추상화/알고리즘 (의사코드 · 순서도/제어흐름)\n"
    "  Atom=한 단계의 동작(대입·조건·반복) / Block=단계들이 이루는 제어구조(순차·선택·반복) / "
    "Relation=제어구조들의 연결(순서도 모양) / Macro=이 알고리즘이 핵심적으로 계산·결정하는 것\n"
    "· 패턴인식 (파이썬 · 규칙 찾기)\n"
    "  Atom=반복 1회에 바뀌는 값 한 개 / Block=반복이 쌓아 만드는 묶음(수열·누적) / "
    "Relation=그 변화가 반복·재등장하는 지점 / Macro=일반 규칙(공식·점화식)\n"
    "· 코드(중간 출력) · 코드(최종 출력) (파이썬 · 출력 추적)\n"
    "  Atom=한 줄 실행 시 변수 변화 / Block=반복 1회·한 묶음 종료 시점의 상태 / "
    "Relation=묶음을 거치며 이어지는 값의 흐름 / Macro=목표 시점의 출력값(중간 또는 최종)\n"
    "  ※ 여기서 Macro는 '실행 결과'의 전체이지 코드의 '목적'이 아니다.\n\n"

    "[진행]\n"
    "1. 현재 문항의 유형과 보는 코드 종류(의사코드/파이썬)를 먼저 확인한다.\n"
    "2. 학생 질문을 보고 그 유형의 어느 단(Atom~Macro)에서 막혔는지 판단한다.\n"
    "3. 그 단에 맞는 유도 질문 하나로 이끈다.\n"
    "4. 학생이 스스로 설명하면 다음 단으로 올리고, 막히면 같은 단을 더 잘게 쪼개 다시 묻는다.\n\n"

    "[말투·형식 — 반드시 일관 유지]\n"
    "- 항상 정중한 존댓말(해요체). 문장 끝은 ~요 / ~까요 / ~네요. 반말(~까?, ~같아?, ~거야 등) 금지.\n"
    "- 답변 전체를 큰따옴표(\")나 작은따옴표(')로 감싸지 않는다. "
    "코드의 변수·함수명 같은 짧은 식별자를 가리킬 때만 따옴표를 쓰고, 그 밖에는 문장만 그대로 출력한다.\n"
    "- 중학생도 이해할 수 있는 쉬운 말로, 2~3문장 이내로 짧게.\n"
    "- 모든 답변은 유도 질문 하나로 끝낸다 (예: 그렇다면 x가 1일 때 이 줄은 어떻게 될까요?).\n"
    "- 학생 질문이 모호하면 어느 부분을 묻는지 되묻는다.\n\n"

    "[절대 금지]\n"
    "- 정답, 정답 코드, 정답 값(출력값)을 직접 알려주지 않는다. '정답은 ~입니다 / A번이 맞습니다' 금지.\n"
    "- 단을 건너뛰어 곧바로 Macro(전체·결과)를 말해버리지 않는다.\n"
    "- '통합·융합' 같은 용어를 새로 끌어들이지 않는다."
)

SYSTEM_CODE = (
    "너는 '코드 읽기' 학습용 파이썬 예제를 만드는 출제자다.\n"
    "대상 수준: 중학교 3학년~고등학교 1학년 (리스트·딕셔너리·문자열, for/while 반복, if 조건, "
    "간단한 함수, 기본 산술까지만. 클래스·재귀·예외처리·컴프리헨션·람다·외부 라이브러리·import 금지).\n"
    "규칙:\n"
    "1. 외부 입력 없이 그대로 실행되는 단일 완결 프로그램. import 금지.\n"
    "   ★ input()을 절대 쓰지 마라 — 처리할 데이터(리스트·딕셔너리·숫자 등)는 코드 안에 값으로 직접 적는다 "
    "(예: data = [10, 20, 30, 40, 50]). 사용자 입력을 받는 코드는 실격이다.\n"
    "2. 함수는 정확히 1개만 정의하고, 그 함수 안에 반복문(for 또는 while)을 1개 이상 둔다.\n"
    "3. ★ 반복문이 한 번 돌 때마다 진행 상황을 print로 출력한다(반복마다 값이 변하는 누적·카운트 변수를 함께 보여준다). "
    "그래서 실행하면 여러 줄이 출력되고, 마지막에 최종 결과도 print한다 — 중간 출력과 최종 출력이 모두 존재해야 한다.\n"
    "4. 길이 최대 20줄(권장 10~16줄). 변수·함수 이름은 의미가 드러나는 영문 snake_case(예: total_price, count_down). "
    "한글 식별자 금지. 주석은 넣지 않는다(실행 추적 문항의 단서가 되지 않도록).\n"
    "5. 아래 참고 예시가 있으면 구조·스타일만 본뜨고 그대로 복사하지 마라.\n"
    "6. 출력은 JSON 객체 1개만: {\"python_code\": \"<실행 가능한 코드 전체>\"}. 코드펜스·설명 문장 금지."
)


# ── 코드 생성 구조화 출력 (python_code 1개) ─────────────────────────
CODE_GEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"python_code": {"type": "string"}},
    "required": ["python_code"],
}
CODE_GEN_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "code_gen", "strict": True, "schema": CODE_GEN_SCHEMA},
}


# ── 의사코드 생성 (python_code → 한글 의사코드 줄 배열) ──────────────
SYSTEM_PSEUDOCODE = (
    "너는 파이썬 코드를 '한글 의사코드'로 번역하는 도구다. 새 알고리즘을 만들지 말고, "
    "주어진 코드와 의미가 똑같게 한 줄씩 번역한다.\n"
    "출력: 각 원소가 의사코드 '한 단계(한 줄)'인 문자열 배열. 블록(반복·조건의 안쪽)은 앞 공백 2칸 들여쓰기로 표현한다.\n"
    "컨벤션:\n"
    "- 함수 정의 → '함수 <이름>(<매개변수>) 정의:'\n"
    "- range 반복 → 'i를 <시작>부터 <끝>까지 반복:' / 목록 순회 → '<목록>의 각 원소 x에 대해 반복:'\n"
    "- while → '<조건>인 동안 반복:'\n"
    "- 조건 → '만약 <조건>이면:' / '아니면:'\n"
    "- 대입·누적 → '<변수>를 <값>으로 둔다' / '<변수>에 <값>을 더한다'\n"
    "- 반환 → '<값> 반환' / 출력 → '<값> 출력'\n\n"
    "예시1 파이썬:\n"
    "def sum_to(n):\n    total = 0\n    for i in range(1, n + 1):\n        total += i\n    return total\nprint(sum_to(5))\n"
    "예시1 pseudocode_lines:\n"
    '["함수 sum_to(n) 정의:", "  total을 0으로 둔다", "  i를 1부터 n까지 반복:", "    total에 i를 더한다", "  total 반환", "sum_to(5)의 결과를 출력"]\n\n'
    "예시2 파이썬:\n"
    "def count_even(nums):\n    count = 0\n    for x in nums:\n        if x % 2 == 0:\n            count += 1\n    return count\nprint(count_even([3, 8, 5, 12]))\n"
    "예시2 pseudocode_lines:\n"
    '["함수 count_even(nums) 정의:", "  count를 0으로 둔다", "  nums의 각 원소 x에 대해 반복:", "    만약 x가 짝수이면:", "      count에 1을 더한다", "  count 반환", "count_even([3, 8, 5, 12])의 결과를 출력"]\n\n'
    "출력은 JSON 객체 1개만: {\"pseudocode_lines\": [\"...\", \"...\"]}. 설명·코드펜스 금지."
)
PSEUDOCODE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "pseudocode_lines": {"type": "array", "minItems": 3, "items": {"type": "string"}},
    },
    "required": ["pseudocode_lines"],
}
PSEUDOCODE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "pseudocode", "strict": True, "schema": PSEUDOCODE_SCHEMA},
}


# ── 유형 3·4·5 문항 생성 (경로 B — LLM + verification_snippet 실행 검증) ──
# 공통: 4지선다. ct_skill·code_kind·answer_type은 서버가 슬롯별로 주입하므로 LLM은
# stem·options(4)·answer_index·verification_snippet·explanation·focus_points만 만든다.
_Q_JSON_TAIL = (
    "\n출력은 JSON 객체 1개만: "
    '{"stem": "...", "options": ["보기1", "보기2", "보기3", "보기4"], "answer_index": 0, '
    '"verification_snippet": "...", "explanation": "정답 해설 1~2문장", "focus_points": ["단서1", "단서2"]}. '
    "코드펜스·설명 문장 금지."
)

SYSTEM_Q_PATTERN = (
    "주어진 파이썬 코드의 반복을 분석해 '패턴인식' 4지선다 1문항을 만든다.\n"
    "코드의 반복 변수(예: i)와 반복마다 값이 변하는 누적/카운트 변수(예: total)를 찾는다.\n"
    "발문(stem)은 '다음 코드에서 {반복변수}가 {대상값}일 때 {누적변수}의 값은?' 형태로, 반복 도중 한 시점을 고른다.\n"
    "verification_snippet: 코드를 그 시점까지 실행해 그 누적변수 값 '하나만' print하는 자족 실행 코드"
    "(필요한 정의 포함, import·input 금지).\n"
    "options 4개: 정답 = 그 시점 누적변수 값, 오답 3개 = 대상값±1 시점 값·반복변수 값 자체·최종 누적값 등 "
    "흔한 추적 실수. 네 보기는 서로 다르게.\n"
    "정답 보기 문자열은 verification_snippet의 print 출력과 글자 그대로 정확히 같게 한다"
    "(단위·접미사·따옴표 금지). answer_index는 정답 보기 위치(0~3)."
    + _Q_JSON_TAIL
)

SYSTEM_Q_MIDOUT = (
    "주어진 파이썬 코드는 실행하면 여러 줄을 출력한다. '코드 중간 출력' 4지선다 1문항을 만든다.\n"
    "체크포인트를 정확히 하나 정한다(예: N번째 반복까지 출력된 내용, 값을 반환하기 직전까지의 출력).\n"
    "발문(stem)에 그 체크포인트를 분명히 쓴다(예: '위 코드에서 3번째 반복까지 출력된 내용은?').\n"
    "verification_snippet: 그 체크포인트까지의 stdout만 그대로 나오게 하는 자족 실행 코드"
    "(코드를 체크포인트까지만 실행하거나 그 출력 줄들을 그대로 print). import·input 금지.\n"
    "options 4개: 정답 = 체크포인트까지의 출력(여러 줄이면 줄바꿈 포함), 오답 3개 = 한 줄 누락/추가·"
    "한 번 더/덜 반복·최종 전체 출력 등. 네 보기는 서로 다르게.\n"
    "정답 보기는 verification_snippet 출력과 글자 그대로 일치. answer_index는 정답 위치(0~3)."
    + _Q_JSON_TAIL
)

SYSTEM_Q_FINOUT = (
    "주어진 파이썬 코드의 '최종 출력' 4지선다 1문항을 만든다.\n"
    "발문(stem)은 고정: '위 코드를 실행하면 최종 출력은?'.\n"
    "verification_snippet: 주어진 코드를 그대로(전체) 실행해 전체 stdout이 나오게 한다.\n"
    "options 4개: 정답 = 코드 전체 실행 출력(여러 줄이면 줄바꿈 포함), 오답 3개 = 중간값을 최종으로 착각·"
    "off-by-one·형식 차이(따옴표·콤마·소수점) 등. 네 보기는 서로 다르게.\n"
    "정답 보기는 실행 출력과 글자 그대로 일치. answer_index는 정답 위치(0~3)."
    + _Q_JSON_TAIL
)

SYSTEM_Q_BY_KIND = {
    "pattern":      SYSTEM_Q_PATTERN,
    "mid_output":   SYSTEM_Q_MIDOUT,
    "final_output": SYSTEM_Q_FINOUT,
}

QGEN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "stem":                 {"type": "string"},
        "options":              {"type": "array", "minItems": 4, "maxItems": 4,
                                 "items": {"type": "string"}},
        "answer_index":         {"type": "integer", "minimum": 0, "maximum": 3},
        "verification_snippet": {"type": "string"},
        "explanation":          {"type": "string"},
        "focus_points":         {"type": "array", "minItems": 1, "maxItems": 3,
                                 "items": {"type": "string"}},
    },
    "required": ["stem", "options", "answer_index", "verification_snippet",
                 "explanation", "focus_points"],
}
QGEN_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "typed_question", "strict": True, "schema": QGEN_SCHEMA},
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
        messages=_skip_think(_ensure_user_turn(_with_lang_guard(messages))),
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
    """코드 제시 자산 생성 — 단일 완결 파이썬 프로그램(함수 1개 + 내부 반복 + 진행 print).
    json_schema로 {"python_code": ...}를 강제한다(api가 파싱). (code_presentation_revision §3)"""
    system = SYSTEM_CODE
    if ctx:
        system += f"\n\n[참고 예시]\n{ctx}"
    user_content = f"난이도: {difficulty} / 주제 힌트: {topic}"
    return _generate(
        [{"role": "system", "content": system},
         {"role": "user",   "content": user_content}],
        TEMP_CREATIVE,
        response_format=CODE_GEN_RESPONSE_FORMAT,
    )


def call_pseudocode_gen(python_code: str) -> str:
    """검증된 python_code를 한글 의사코드 줄 배열로 번역. json_schema 강제(api가 파싱).
    (code_presentation_revision §4 — 유형 1 문제분해의 입력)"""
    return _generate(
        [{"role": "system", "content": SYSTEM_PSEUDOCODE},
         {"role": "user",   "content": f"[파이썬 코드]\n{python_code}"}],
        TEMP_CREATIVE,
        max_tokens=1536,
        response_format=PSEUDOCODE_RESPONSE_FORMAT,
    )


def call_typed_question_gen(kind: str, python_code: str, templates: str = "") -> str:
    """유형 3·4·5(패턴인식/중간출력/최종출력) 단일 문항 생성. json_schema 강제(생성·재생성 공용).
    kind: 'pattern' | 'mid_output' | 'final_output'."""
    system = SYSTEM_Q_BY_KIND[kind]
    if templates:
        system += f"\n\n[출제 참고 가이드]\n{templates}"
    return _generate(
        [{"role": "system", "content": system},
         {"role": "user",   "content": f"[파이썬 코드]\n{python_code}"}],
        TEMP_CREATIVE,
        max_tokens=2048,
        response_format=QGEN_RESPONSE_FORMAT,
    )


# ── 해설 교정 (확정 정답에 맞춰 해설 재작성) ────────────────────────
# 유형 3·4·5는 정답을 snippet 실행값으로 '확정'하지만, 해설은 LLM이 확정 전에 써서
# 정답과 어긋날 수 있다. 불일치가 감지되면 '확정 정답'을 근거로 해설만 다시 쓴다.
SYSTEM_EXPLAIN_FIX = (
    "너는 객관식 코드 문항의 '정답 해설'을 쓰는 도구다. 정답은 이미 코드의 실제 실행으로 확정돼 있다.\n"
    "[확정 정답]이 왜 정답인지 학생에게 1~2문장으로 짧고 정중하게(해요체/입니다체) 설명한다.\n"
    "규칙:\n"
    "1. 반드시 [확정 정답]과 일치하게 설명한다. 다른 값을 정답이라고 말하지 마라.\n"
    "2. 해설에 결과 수치를 쓸 때는 [확정 정답]의 값과 글자 그대로 똑같이 쓴다 — 다른 숫자를 결론으로 내지 마라.\n"
    "3. 코드의 동작(반복·누적·출력 순서 등)에 근거해 설명하되, 풀이 과정을 장황히 늘어놓지 않는다.\n"
    "출력은 JSON 객체 1개만: {\"explanation\": \"...\"}. 코드펜스·설명 문장 금지."
)
EXPLAIN_FIX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"explanation": {"type": "string"}},
    "required": ["explanation"],
}
EXPLAIN_FIX_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "explanation_fix", "strict": True, "schema": EXPLAIN_FIX_SCHEMA},
}


def call_explanation_fix(python_code: str, stem: str, correct_answer: str) -> str:
    """확정 정답에 맞춰 해설을 재작성. json_schema로 {"explanation": ...} 강제(api가 파싱).
    분석 계열 온도(TEMP_PRECISE)로 정답 충실도를 높인다."""
    user = (
        f"[파이썬 코드]\n{python_code}\n\n"
        f"[문제]\n{stem}\n\n"
        f"[확정 정답]\n{correct_answer}"
    )
    return _generate(
        [{"role": "system", "content": SYSTEM_EXPLAIN_FIX},
         {"role": "user",   "content": user}],
        TEMP_PRECISE,
        max_tokens=400,
        response_format=EXPLAIN_FIX_RESPONSE_FORMAT,
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
                      ct_skill: str = None, focus_points=None, code_kind: str = None) -> str:
    system = SYSTEM_CHAT
    if code_context:
        system += f"\n\n[현재 학습 중인 코드]\n{code_context}"
    if current_problem:
        system += f"\n\n[현재 풀고 있는 문제]\n{current_problem}"
    if code_kind:
        kind_label = {"pseudocode": "의사코드", "python": "파이썬"}.get(code_kind, code_kind)
        system += (
            f"\n\n[이 문항이 보는 코드 종류] {kind_label}\n"
            "힌트는 이 코드 종류에 근거해 준다. 의사코드 문항이면 파이썬 문법이 아니라 "
            "단계(줄)의 의미로, 파이썬 문항이면 실제 실행되는 코드 줄로 단서를 잡아라."
        )
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


def build_trigger_user_message(trigger_type: str, ct_skill: str = None) -> str:
    """챗봇이 먼저 말하도록 만드는 유사-유저 메시지. 학생 발화로 기록하지 않는다."""
    if trigger_type == "hint":
        msg = (
            "[학생 행동: 힌트 버튼 클릭]\n"
            "학생이 현재 문제에 대한 힌트를 요청했다.\n"
        )
        if ct_skill:
            msg += f"이 문항의 컴퓨팅 사고력 요소는 '{ct_skill}'이다.\n"
        msg += (
            "학생이 아직 아무 말도 하지 않았으니, Block Model 사다리의 가장 낮은 단(Atom: "
            "한 줄·한 요소의 의미)에서 시작하라. 정답·정답 라벨을 절대 말하지 말고, 이 요소의 "
            "Atom 단에 맞는 생각해볼 거리를 소크라테스식 유도 질문 한 개로만 던져라. 2~3문장 이내. "
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
        messages=_skip_think(_ensure_user_turn(_with_lang_guard(messages))),
        max_tokens=4096,
        temperature=TEMP_CREATIVE,
        stream=True,
    )
