# 생성형 점진적 프롬프팅을 통한 컴퓨팅 사고력 향상 프로그램

캡스톤 프로젝트 | Flask 기반 Python 학습 보조 웹앱 | 제주도 중·고등학생 대상(중·고 통합 난이도)

---

## 프로젝트 정체성 (가장 먼저 읽을 것)

- **목표(측정 대상)**: 컴퓨팅 사고력(CT) 향상. 이것이 이 프로젝트의 핵심이다.
- **방법(수단)**: 생성형 점진적 프롬프팅 — 학생이 소크라테스 챗봇과 대화하며 질문하는 과정.
- **CT 측정은 대화 로그 분석으로 한다. 이것이 연구의 심장이다.**
- 프롬프팅 품질 평가는 **보조 지표**다. CT 측정이 주(主), 프롬프팅이 부(副).

제목 세 단어의 대응:
- 생성형 = LLM이 코드·문제·질문을 매번 새로 생성 (고정 문제 은행 아님)
- 점진적 = 세션 내(난이도 상승) + **세션 간(CT 약점이 다음 세션에 반영되는 루프)**
- 프롬프팅 = 학생이 AI에게 질문하는 행위 자체가 학습 수단

---

## 프로그램 구조

Flask 웹앱. Python 학습 보조 도구.

### 전체 흐름
1. 사용자가 학습 단계(stage)에 진입하면 해당 주제의 Python 코드 예제 생성 (LM Studio 로컬 LLM)
2. 그 코드를 기반으로 4지선다 객관식 문제 5개를 하나씩 순차 생성 (CT 요소 순서 = 난이도 매우쉬움→매우어려움)
3. 학생이 보기를 선택해 제출하면 코드로 즉시 채점 후 다음 문제 생성
4. 문제 풀이 사이사이 소크라테스 챗봇과 대화 (정답 미제공, 유도 질문만)
5. 세션 완료 시 **대화 로그 분석으로 CT 4요소 측정** + 프롬프팅 품질 보조 평가
6. CT 약점을 저장 → **다음 세션 챗봇 유도에 반영** (세션 간 점진성)

### LLM 연결
- LM Studio (localhost:1234/v1), OpenAI SDK 호환
- 채팅 모델: local-model (현재 Qwen3 8B)
- 임베딩 모델: text-embedding-bge-m3 (RAG용)

### RAG 구조
- 라이브러리: LangChain + FAISS
- rag_docs/code_examples/      ← 코드 예제 참고 TXT (주제별) **작성 완료**
- rag_docs/problem_templates/  ← 문제 출제 템플릿 TXT (CT 요소별) **작성 완료**
- rag_db/                      ← FAISS 인덱스 저장 (자동 생성)
- 청크 크기: 200자, 오버랩: 50자, TOP_K: 5

### 주요 파일
- app.py                       : Flask 앱 진입점
- api.py                       : 모든 API 엔드포인트 (Blueprint)
- rag.py                       : RAG 모듈 (FAISS 인덱싱 + 검색)
- llm.py                       : 모든 LLM 호출 중앙 모듈 (역할별 분리) ☐ 구현 필요
- static/main.js               : 프론트엔드 로직
- templates/index.html         : UI

---

## 핵심 설계 ① — CT 측정 (연구의 핵심)

세션의 전체 대화 로그를 분석하여 CT 4요소 점수를 산출한다. 이것이 주 측정이다.

### CT 4요소 (측정 대상)
| CT 요소 | 대화에서 관찰하는 행동 |
|---------|------------------------|
| 분해 | 문제를 부분으로 나눠 질문했는가 |
| 패턴인식 | 반복·규칙성을 발견하는 질문을 했는가 |
| 추상화 | 변수·함수의 역할을 일반화해 사고했는가 |
| 알고리즘적사고 | 실행 순서·흐름을 추적하는 질문을 했는가 |

- call_log_analysis가 대화 로그 + CT 루브릭으로 4요소 각각 점수화(예: 1~5점) + 근거 + 약점 도출.
- 출력은 JSON. temperature 0.2(일관성).
- 프롬프팅 품질 평가(call_prompt_eval)는 보조 지표로 함께 산출하되, 약점 판정·세션 간 반영의 기준은 **CT 4요소**다.

---

## 핵심 설계 ② — 세션 간 점진성 (수준 1+2)

"점진적"의 실체. CT 약점이 다음 세션 학습에 반영되는 루프를 구현한다.

### 데이터 흐름
```
세션 N 종료
  → 대화 로그 분석 → CT 4요소 점수 (feedback 테이블에 JSON 저장)
  → 가장 약한 CT 요소 추출 (예: "추상화" 2점)
        ↓
세션 N+1 시작
  → [수준 2] 약한 CT 요소를 챗봇 시스템 프롬프트에 주입
       "이 학생은 추상화가 약함. 변수·함수의 역할을 일반화해
        생각하도록 유도하는 질문을 우선하라."
  → [수준 1] 시작 화면에 지난 CT 점수 + 약한 요소 + 개선 포인트 표시
        ↓
세션 N+1 진행 → 다시 CT 측정 → 또 반영 (루프 반복)
```

### 수준 1 — CT 피드백 표시
- 세션 시작 화면에 직전 세션의 CT 4요소 점수와 가장 약한 요소를 보여준다.
- 예: "지난번 '추상화'가 약했어요. 이번엔 변수가 무엇을 대표하는지 질문해보세요."

### 수준 2 — 약점 기반 챗봇 유도 (핵심)
- 직전 세션의 약한 CT 요소를 다음 세션 챗봇 시스템 프롬프트에 주입한다.
- 챗봇이 그 요소를 집중적으로 끌어내는 유도 질문을 우선한다.
- 이 "약점 데이터 → 챗봇 행동 변화" 루프가 연구의 점진성 주장 근거다.

### 확장 여지 (지금은 구현 안 함)
- 수준 3(CT 점수에 따른 난이도·집중요소 자동 조정)은 코드 구조만 확장 가능하게 열어둔다.
- feedback 테이블의 CT 점수를 읽어 stage 시작점이나 집중 CT를 조정하는 식으로 추후 덧붙임.
- 실험 규모(다수 학생·여러 세션)가 확보되면 도입 검토. 현재 범위 밖.

### 첫 세션 처리
- 직전 세션 데이터가 없으면(최초 사용) 약점 주입 없이 표준 챗봇 프롬프트로 시작한다.

---

## 핵심 설계 ③ — 2차원 구조 (단계 × CT)

문제 출제는 두 개의 독립적인 축으로 구성된다.

### 축 1 — 학습 단계 (stage, 주제 진행) : 총 4단계
| stage | 주제 | 교육과정 근거 | 수준 |
|-------|------|--------------|------|
| 0 | 변수·연산·조건 | 9정03-06 | 중학 |
| 1 | 반복·리스트 | 9정03-05, 06 | 중학 |
| 2 | 함수 | 9정03-07 | 중~고 |
| 3 | 알고리즘(정렬·탐색) | 12정03 | 고등 |

융합은 단계에 포함되지 않는다(아래 "융합 종합 평가" 참조).
중·고 통합: stage 0~2는 중학생도 따라오고, stage 3는 고등 내용이지만 각 단계 내 5문제가 매우쉬움→매우어려움으로 진행되어 자연스럽게 통합 난이도가 된다.

### 축 2 — CT 요소 (problem_index, 각 단계 내 5문제)
| problem_index | CT 요소 | 난이도 | 질문 방향 |
|---------------|---------|--------|-----------|
| 0 | 분해 | 매우쉬움 | 코드를 역할별로 나눈다면? |
| 1 | 패턴인식 | 쉬움 | 출력 패턴의 다음 값은? |
| 2 | 추상화 | 보통 | 이 변수가 의미하는 것은? |
| 3 | 알고리즘적사고 | 어려움 | n번 반복 후 변수 값은? |
| 4 | 통합 | 매우어려움 | 이 코드가 해결하는 문제는? |

CT 5단계 순서·단계 순서는 인지 부하 이론 기반으로 변경하지 않는다.

### 두 축의 결합
```
stage=1, problem_index=2  →  "반복·리스트" 주제 코드 + "추상화" 문제 (보통 난이도)
```
- 한 단계당 코드 1개 + CT 5문제. 4단계 × 5문제 = 정규 20문제.

---

## 융합 종합 평가 (4단계 완료 후, 별도)

4개 단계를 모두 마친 뒤 진행하는 종합 평가. 정규 단계와 분리된다.
- 코드: 여러 개념(조건+반복+함수+알고리즘)을 2개 이상 결합한 종합 코드
- 문제: 동일하게 객관식 CT 5문제(분해→통합)
- 근거: 9정03-08(실생활 통합) / 12정03

### ⚠️ 이름 구분 (혼동 주의)
- **통합** = CT 요소 5번째(각 단계 problem_index=4). 한 코드의 전체 목적 파악.
- **융합** = 4단계 완료 후의 종합 평가 단계. 여러 개념 결합 코드.
- 서로 다른 축이다. 변수·함수·파일명에서 섞지 말 것.

---

## 핵심 설계 ④ — 문제는 4지선다 객관식 (채점은 코드 기반)

서술형 자동 채점은 로컬 8B 모델에서 일관성이 깨지므로 **객관식으로 고정**한다.
채점은 `정답 == 학생선택` 라벨 비교로만 하며 **LLM을 거치지 않는다.**
객관식은 이해도 게이트키퍼, **실제 CT 측정은 대화 로그 분석이 담당**한다(역할 분리).

### 문제 JSON 규격 (고정)
```json
{
  "question": "문제 본문",
  "options": ["A. 보기1", "B. 보기2", "C. 보기3", "D. 보기4"],
  "answer": "B",
  "explanation": "정답 해설 1~2문장",
  "ct_skill": "추상화",
  "difficulty": "보통"
}
```
- 보기 정확히 4개(A/B/C/D), 정답 1개. 오답 3개는 그럴듯하지만 명백히 틀리게.
- 정답 위치가 특정 라벨에 쏠리지 않게 다양화.

---

## 핵심 설계 ⑤ — LLM 역할 분리 (llm.py)

LLM은 stateless다. 같은 모델 1개라도 시스템 프롬프트·temperature·history로 역할을 나눈다.
모든 LLM 호출을 llm.py 하나로 모으고, api.py 등은 이 함수들만 호출한다.

### 우려 대응
1. **히스토리 격리**: call_chatbot만 history를 받는다. 분석 함수는 대화 로그를 messages 맥락이 아니라 단일 user 메시지의 "분석 대상 자료"로 전달 → 페르소나 오염 방지.
2. **temperature 차등**: 생성 계열 창의값, 분석 계열 정밀값.
3. **동시 호출**: 모든 호출을 단일 진입점 `_generate`로 모으고 threading.Lock으로 직렬화.

### llm.py 구조
```python
import threading
from openai import OpenAI

client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")
MODEL = "local-model"

TEMP_CREATIVE = 0.6   # 챗봇, 코드/문제 생성 (객관식 형식 안정 위해 0.5~0.7)
TEMP_PRECISE  = 0.2   # 대화 분석(CT 측정), 프롬프트 평가

_llm_lock = threading.Lock()

def _generate(messages, temperature):
    with _llm_lock:
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=temperature
        )
    return resp.choices[0].message.content

# 생성 계열 (TEMP_CREATIVE)
def call_chatbot(history, user_msg, code, problem, weak_ct=None): ...
    # weak_ct: 직전 세션의 약한 CT 요소(수준 2). 있으면 시스템 프롬프트에 유도 지침 주입.
    # history 받는 유일한 함수.
def call_code_gen(topic, code_refs): ...                  # history 없음
def call_problem_gen(code, ct_skill, difficulty, templates, previous): ...  # history 없음

# 분석 계열 (TEMP_PRECISE, history 절대 미주입, 로그는 단일 user 메시지)
def call_log_analysis(chat_log_text, ct_rubric): ...      # CT 4요소 측정 (핵심)
def call_prompt_eval(chat_log_text, prompt_rubric): ...   # 프롬프팅 품질 (보조)
```

| 함수 | 용도 | temperature | history | 비고 |
|------|------|-------------|---------|------|
| call_chatbot | 소크라테스 챗봇 | 0.6 | O (유일) | weak_ct 주입(수준 2) |
| call_code_gen | 코드 생성 | 0.6 | X | |
| call_problem_gen | 문제 출제 | 0.6 | X | |
| call_log_analysis | **CT 측정(핵심)** | 0.2 | X | 로그=단일 user 메시지 |
| call_prompt_eval | 프롬프팅 평가(보조) | 0.2 | X | 로그=단일 user 메시지 |

---

## RAG 검색 규칙

두 RAG 폴더는 서로 다른 축으로 검색한다. (평가·챗봇 기능엔 RAG 미적용)

### code_examples ← stage(주제) 또는 융합으로 검색
```python
topic = STAGE_MAP[stage]
code_refs = rag_store.retrieve("code_examples", topic)
# 융합: rag_store.retrieve("code_examples", "융합")
```

### problem_templates ← problem_index(CT 요소)로 검색  [작성 완료]
```python
ct_skill   = CT_MAP[problem_index]["ct_skill"]
difficulty = CT_MAP[problem_index]["difficulty"]
templates  = rag_store.retrieve("problem_templates", f"{ct_skill} {difficulty} {code[:300]}")
```

### 평가·챗봇 기능엔 RAG를 적용하지 않는다
- CT 측정·프롬프팅 평가의 루브릭은 고정·소량 → RAG 대신 시스템 프롬프트에 직접 주입.
- 챗봇도 RAG 미적용(지식 문서 주입 시 정답 누설 위험). 시스템 프롬프트 + 코드/문제 맥락 + (수준 2)약점만.
- (선택) 채점된 예시 코퍼스가 쌓이면 "예시 검색" RAG로 CT 채점 일관성 보정 가능 — 현재 범위 밖.

---

## 파일 구조

```
project/
├── app.py
├── api.py
├── rag.py
├── llm.py                        # ☐ 구현 필요
├── CLAUDE.md
│
├── rag_docs/
│   ├── problem_templates/        # ★ 작성 완료 — 수정 금지
│   │   ├── ct_분해.txt
│   │   ├── ct_패턴인식.txt
│   │   ├── ct_추상화.txt
│   │   ├── ct_알고리즘적사고.txt
│   │   └── ct_통합.txt
│   └── code_examples/            # ★ 작성 완료
│       ├── 순차조건.txt          # stage 0 (변수·연산·조건)
│       ├── 반복리스트.txt        # stage 1
│       ├── 함수.txt              # stage 2
│       ├── 알고리즘.txt          # stage 3
│       └── 융합.txt              # 융합 종합 평가용
│
├── rag_db/                       # 자동 생성
├── static/main.js
└── templates/index.html
```

---

## 작업 목록 (Claude Code가 순서대로 구현)

### Task 1 — llm.py 중앙 모듈 생성
- 위 llm.py 구조대로 작성. 시스템 프롬프트는 llm.py 상단 또는 prompts.py로 분리.
- 기존 api.py에 흩어진 client.chat.completions.create 직접 호출을 모두 llm.py 함수로 이관.
- call_chatbot에 weak_ct 파라미터 추가(수준 2). 있으면 해당 CT 요소를 끌어내는 유도 지침을 시스템 프롬프트에 주입, 없으면 표준 프롬프트.
- 히스토리 격리·동시 호출 Lock 확인.

### Task 2 — CT 측정 분석 모듈 (핵심)
- call_log_analysis: 대화 로그 + CT 루브릭 → CT 4요소 점수 + 근거 + 가장 약한 요소를 JSON으로 산출.
- call_prompt_eval: 프롬프팅 6요소 보조 점수 산출.
- 세션 종료 시 두 결과를 feedback 테이블에 JSON 저장(가장 약한 CT 요소 필드 포함).

### Task 3 — 세션 간 점진성 루프 (수준 1+2)
- 세션 시작 시 직전 세션의 feedback에서 약한 CT 요소를 읽는다.
- [수준 2] 그 요소를 call_chatbot(weak_ct=...)로 전달해 챗봇 유도에 반영.
- [수준 1] 시작 화면에 직전 CT 점수 + 약한 요소 + 개선 포인트 표시.
- 직전 데이터가 없으면(최초) 약점 주입 없이 표준 진행.

### Task 4 — api.py 2차원 매핑 적용
```python
STAGE_MAP = {0: "변수·연산·조건", 1: "반복·리스트", 2: "함수", 3: "알고리즘"}
FUSION_TOPIC = "융합"
CT_MAP = {
    0: {"ct_skill": "분해",          "difficulty": "매우쉬움"},
    1: {"ct_skill": "패턴인식",      "difficulty": "쉬움"},
    2: {"ct_skill": "추상화",        "difficulty": "보통"},
    3: {"ct_skill": "알고리즘적사고", "difficulty": "어려움"},
    4: {"ct_skill": "통합",          "difficulty": "매우어려움"},
}
```
- 코드 생성: stage(또는 융합 플래그) → topic → code_examples 검색.
- 문제 생성: problem_index → ct_skill/difficulty → problem_templates 검색.

### Task 5 — 문제 출제 객관식 전환
- call_problem_gen 시스템 프롬프트에 "4지선다 객관식 강제 + JSON 형식" 명시.
- 응답 파싱을 "첫 줄 추출" → JSON 파싱(첫 { ~ 마지막 } 추출, 보기 4개·정답 라벨 검증)으로 교체.
- 파싱 실패 시 1회 재생성.

### Task 6 — 채점 로직 코드 기반으로 교체
- 학생 답안 채점을 `정답 == 학생선택` 라벨 비교로 구현. LLM 호출 없음.
- 해설은 미리 생성된 explanation을 그대로 표시.

### Task 7 — 프론트엔드
- 문제: 텍스트 입력 → 보기 4개 선택 UI(라디오 등). 라벨(A~D) 전송, 정오·해설 표시.
- 세션 시작 화면: 직전 CT 점수 + 약한 요소 + 개선 포인트 표시(수준 1).

### Task 8 — rag.py 청크 크기 분리 (선택)
- code_examples 컬렉션만 청크 400~500자. problem_templates는 200자 유지.

### Task 9 — FAISS 재빌드 + 동작 확인
- rag_db/ 삭제 후 재빌드.
- stage 0~3 코드 생성 / 융합 결합 코드 생성 확인.
- 객관식 JSON 안정 파싱, 라벨 채점 정확, 정답 위치 편향 없음.
- CT 측정이 4요소 점수 + 약점을 일관된 JSON으로 산출.
- 세션 간 루프: 직전 약점이 다음 세션 챗봇 프롬프트에 실제로 반영되는지 확인.
- 챗봇이 질문으로 끝나고 정답 누설 안 함.

---

## 주의사항

1. **CT 측정(대화 로그 분석)이 연구의 핵심.** 프롬프팅 평가는 보조 지표.
2. **세션 간 점진성의 기준은 CT 4요소.** 약점 판정·챗봇 유도 모두 CT 기준(프롬프팅 아님).
3. **problem_templates/ 와 code_examples/ 는 작성 완료. 수정 금지.**
4. **단계는 4개.** 융합은 단계가 아니라 4단계 후 별도 종합 평가.
5. **통합(CT 5번째) ≠ 융합(평가 단계).** 혼동 금지.
6. **CT 5단계·단계 순서** 변경 금지.
7. **채점에 LLM 사용 금지.** 객관식 라벨 비교가 전부.
8. **분석 LLM에 챗봇 history 주입 금지.** 로그는 분석 대상 자료로만 전달.
9. **모든 LLM 호출은 llm.py의 _generate(Lock)를 거친다.**
10. **평가·챗봇 기능엔 RAG 미적용.**
11. **소크라테스 챗봇 "정답 금지" 규칙** 변경 금지.
12. 수준 3(적응형 난이도)은 현재 범위 밖. 구조만 확장 가능하게 둔다.

---

## 현재 개발 상태

- [x] problem_templates CT 가이드 TXT 5개 작성 완료
- [x] code_examples 주제별 TXT 5개 작성 완료
- [ ] Task 1: llm.py 중앙 모듈 (역할 분리 + Lock + 히스토리 격리 + weak_ct)
- [ ] Task 2: CT 측정 분석 모듈 (핵심) + feedback 저장
- [ ] Task 3: 세션 간 점진성 루프 (수준 1+2)
- [ ] Task 4: api.py 2차원 매핑
- [ ] Task 5: 문제 출제 객관식 전환
- [ ] Task 6: 채점 로직 코드 기반 교체
- [ ] Task 7: 프론트엔드 (객관식 UI + 세션 시작 화면 CT 피드백)
- [ ] Task 8: rag.py 청크 크기 분리 (선택)
- [ ] Task 9: FAISS 재빌드 + 동작 확인
