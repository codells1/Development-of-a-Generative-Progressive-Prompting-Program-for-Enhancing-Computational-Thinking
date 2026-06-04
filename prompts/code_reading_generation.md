# 코드 읽기 문제 생성 사양 (Code-Reading Item Generation Spec)

> 한 번의 세션에서 **완결된 파이썬 코드 1개 + 객관식(MCQ) 5문항**을 생성하기 위한 사양서.
> 문항은 4지선다이며, 일부 문항은 **코드를 실제로 실행해 정답을 검증**한다.
> 대상: LM Studio 로컬 **Qwen3 8B**(`model="local-model"`, `http://localhost:1234/v1`).

---

## 0. 현 코드와의 차이 (Claude Code 참고)

이 사양은 다음을 전제로 한다. 기존 코드는 아래로 바뀌어야 한다.

- 문제를 `problem_index` 0→4로 **5번 개별 호출**하던 방식 → **한 번에 5문항 세트** 생성
- `CT_MAP`의 **문항별 난이도 상승(매우쉬움~매우어려움)** → **세트 단위 난이도 1개**
- `stage`/`CODE_TOPICS` 기반 코드 생성 → **단일 완결 프로그램** 생성
- 문제 JSON에 **`answer_type` · `verification_snippet` · `focus_points`** 필드 추가

---

## 1. 산출 흐름 (2단계)

`/api/session/start`가 아래 두 단계를 묶어 호출하고, 합친 결과를 반환한다.

```
Stage 1  코드 생성    : RAG(code_examples 참고) → 완결된 파이썬 코드 1개
Stage 2  문항 생성    : 위 코드 + RAG(problem_templates 참고) → MCQ 5문항(JSON 배열)
검증     코드 실행    : answer_type="computational" 문항만 실행해 정답 일치 확인 (서버 로직)
```

> 작은 모델(Qwen3 8B) 안정성을 위해 코드와 문항을 **두 번의 LLM 호출**로 나눈다. 한 호출에 전부 넣지 말 것.

---

## 2. Stage 1 — 코드 생성 규칙

### 2.1 코드의 조건
| 항목 | 기준 |
|---|---|
| 형태 | 실행 가능한 **단일 완결 프로그램** (조각 ❌) |
| 길이 | **최대 30줄 (약 15~30줄)** |
| 함수 | **1~2개** (분해·통합 문항을 위해 **2개 권장**) |
| 의존성 | 표준 라이브러리만, **외부 입력 없이** 실행 (데이터는 코드 안에 고정, `input()` 금지) |
| 이름 | 변수·함수 이름은 의미가 드러나게 |
| 주석 | 학생용 코드에는 최소화 |
| 결과 | `print`로 사람이 읽을 결과 출력 |

> **길이 주의**: 30줄을 넘기지 말 것. 중·고생이 한 화면에서 읽고 5개 사고력을 끌어낼 수 있는 분량이 목표다. 단, 10줄 미만으로 너무 짧으면 통합·분해 문항의 '재료'가 부족해지니 **하한 15줄**을 권장한다. 길이는 줄 수보다 "2.2 구성요소가 다 들어갔는가"로 판단한다.

### 2.2 5유형을 담기 위한 필수 구성요소 (생성 후 자체 점검)
- 구분되는 함수 또는 처리 단계가 2개 이상 (분해)
- 반복문 또는 유사 구조 반복 (패턴인식)
- 함수/매개변수로 세부를 감춘 부분 (추상화)
- 조건 분기 또는 순차 처리 절차 (알고리즘)
- 부분들이 모여 하나의 목적을 이루는 구조 (통합)

> 함수가 1개뿐이면 분해·통합 문항이 약해진다. 가능하면 **함수 2개**로 역할을 나누는 것을 권장한다.

### 2.3 RAG
`rag_docs/code_examples/`의 통합형 예제를 검색해 "참고 예시"로 프롬프트에 넣되, **구조·스타일만 본뜨고 그대로 복사 금지**. 검색 쿼리는 폐기된 stage 키워드 대신 **주제/개념 키워드**(예: "리스트 합계 처리", "딕셔너리 집계")로.

---

## 3. Stage 2 — MCQ 5문항 일괄 생성 규칙

### 3.1 공통
- 한 코드에 대해 **5문항을 한 번에** 생성: `분해 · 패턴인식 · 추상화 · 알고리즘적사고 · 통합` 각 1문항. (이름은 `rag_docs/problem_templates/`의 가이드 파일명과 일치)
- 각 문항 **4지선다(A~D)**, 정답 1개.
- 문항은 **코드를 읽으면 풀 수 있어야** 한다.
- 유형별 출제 방향은 `rag_docs/problem_templates/ct_*.txt`를 RAG로 참조.

### 3.2 answer_type — 검증 가능 여부 분류 (핵심)
각 문항을 둘 중 하나로 분류한다.

- **`computational`**: 코드를 실행하면 **값이 하나로 정해지는** 문항. 예) 출력값, n번 반복 후 변수값, 함수 반환값.
  → `verification_snippet` **필수**.
- **`conceptual`**: 코드의 의미·구조·역할·목적을 묻는 문항. 예) 함수 역할, 추상화 대상, 전체 목적.
  → `verification_snippet`은 **빈 문자열 `""`**.

> 경향(강제 아님): 보통 알고리즘적사고(가끔 패턴인식)가 `computational`, 분해·추상화·통합은 `conceptual`. 모든 문항을 억지로 computational로 만들지 말 것.

### 3.3 verification_snippet 규칙 (computational 전용)
- **자족 실행 코드**: 문제의 정답 값을 구하는 데 필요한 코드(관련 함수 정의 포함)를 담고, **정답 값 하나만 `print`** 한다.
- `input()`·파일·네트워크·무한루프 금지. 출력은 정답 값 한 줄.
- **보기(option)의 값과 형식을 맞춘다.** 예: 보기가 `B. 12000`이면 스니펫은 `12000`을 출력. (서버가 stdout과 정답 보기의 값 부분을 비교한다 → 3.4)

### 3.4 코드 실행 검증 (서버 로직 — LLM 아님)
`computational` 문항에 대해 서버가 수행:
1. `verification_snippet`을 **격리 실행**: 별도 subprocess, **타임아웃 5초**, stdout만 캡처, import/파일/네트워크 차단 권장.
2. 출력값(`stdout.strip()`)을 **정답으로 지정된 보기의 값 부분**과 trim 후 문자열 비교.
3. 판정:
   - 정답 보기와 **일치** → 통과.
   - **불일치**(값이 정답 보기와 다름 / 다른 보기와 일치 / 어느 보기와도 불일치) → 그 문항만 **최대 3회 재생성**.
   - 3회 후에도 실패 → 그 문항 **건너뛰기**(스킵 로그 남김).
4. **★ 금지: 정답 보기를 실행값으로 덮어쓰지 말 것.** 반드시 재생성으로 해결. (덮어쓰면 오답 보기 3개가 근거 없는 값이 됨)

`conceptual` 문항은 실행하지 않고 LLM이 지정한 정답을 그대로 둔다.

### 3.5 focus_points
- 각 문항에 `focus_points`(채점·챗봇 유도용 핵심 포인트, 1~3개)를 생성.
- **학생에게 가는 응답 JSON에서는 제거**한다. 챗봇 시스템 프롬프트(`build_chat_system`)에 **현재 문항의 focus_points만** 주입해 유도 질문 방향을 잡는다. 학생에게 정답으로 노출 금지.

---

## 4. 출력 JSON 스키마

`/api/session/start` 최종 반환(서버 내부 보관용 — 전체 필드):

```json
{
  "title": "프로그램 제목",
  "summary": "이 프로그램이 하는 일 한 줄",
  "difficulty": "중3",
  "code": "실행 가능한 파이썬 코드 전체 (\\n으로 줄바꿈)",
  "questions": [
    {
      "ct_skill": "분해",
      "question": "발문",
      "options": ["A. 보기1", "B. 보기2", "C. 보기3", "D. 보기4"],
      "answer": "B",
      "answer_type": "conceptual",
      "verification_snippet": "",
      "explanation": "정답 해설 1~2문장",
      "focus_points": ["핵심 포인트1", "핵심 포인트2"]
    }
  ]
}
```

- `questions`는 정확히 5개, `ct_skill` 순서: 분해 → 패턴인식 → 추상화 → 알고리즘적사고 → 통합.
- `difficulty`는 **세트 단위 1개**(문항별 난이도 필드 없음).

**학생에게 보낼 때**(프론트 전송): 각 문항에서 `focus_points`, `verification_snippet`, `answer_type` **제거**. (`answer`는 기존대로 클라이언트 채점에 사용하므로 유지.)

---

## 5. 완성 예시 (Few-shot · 프롬프트에 함께 넣을 것)

> 코드 20줄, 함수 2개. 합계 12000 → 등급 "소액". 검증 스니펫 출력은 `12000`. (실행 확인 완료)

```json
{
  "title": "장바구니 합계와 주문 등급",
  "summary": "장바구니 상품들의 합계를 구하고 금액에 따라 주문 등급을 매긴다.",
  "difficulty": "중3",
  "code": "def total_price(cart):\n    total = 0\n    for item in cart:\n        total += item[\"price\"] * item[\"qty\"]\n    return total\n\ndef grade_order(total):\n    if total >= 30000:\n        return \"VIP\"\n    elif total >= 15000:\n        return \"일반\"\n    return \"소액\"\n\ncart = [\n    {\"name\": \"공책\", \"price\": 3000, \"qty\": 2},\n    {\"name\": \"펜\", \"price\": 1500, \"qty\": 4},\n]\ntotal = total_price(cart)\nprint(f\"주문 합계: {total}원\")\nprint(f\"등급: {grade_order(total)}\")",
  "questions": [
    {
      "ct_skill": "분해",
      "question": "이 프로그램은 두 함수로 나뉘어 있습니다. 각 함수의 역할을 바르게 짝지은 것은?",
      "options": [
        "A. total_price=등급 판정, grade_order=합계 계산",
        "B. total_price=합계 계산, grade_order=등급 판정",
        "C. total_price=출력, grade_order=합계 계산",
        "D. 두 함수는 같은 일을 한다"
      ],
      "answer": "B",
      "answer_type": "conceptual",
      "verification_snippet": "",
      "explanation": "total_price는 가격×수량을 더해 합계를, grade_order는 그 합계로 등급을 정한다.",
      "focus_points": ["total_price는 합계를 구한다", "grade_order는 금액으로 등급을 정한다"]
    },
    {
      "ct_skill": "패턴인식",
      "question": "total_price의 반복문에서 반복적으로 일어나는 동작으로 가장 알맞은 것은?",
      "options": [
        "A. 각 상품의 (가격×수량)을 total에 더한다",
        "B. 각 상품의 이름을 출력한다",
        "C. 가장 비싼 상품을 찾는다",
        "D. 상품을 가격순으로 정렬한다"
      ],
      "answer": "A",
      "answer_type": "conceptual",
      "verification_snippet": "",
      "explanation": "for가 cart의 원소를 차례로 꺼내 (가격×수량)을 total에 누적한다.",
      "focus_points": ["item['price']*item['qty']을 누적", "cart 원소를 차례로 순회"]
    },
    {
      "ct_skill": "추상화",
      "question": "grade_order 함수가 숨기고(추상화하고) 있는 것은?",
      "options": [
        "A. 합계를 계산하는 과정",
        "B. 등급을 정하는 구체적인 금액 기준(구간)",
        "C. 상품의 이름",
        "D. 반복문의 동작"
      ],
      "answer": "B",
      "answer_type": "conceptual",
      "verification_snippet": "",
      "explanation": "if-elif 금액 구간 규칙이 함수 뒤로 감춰져, 호출부는 '등급 판정'이라는 의도만 안다.",
      "focus_points": ["금액 구간 if-elif가 함수 안에 캡슐화됨", "호출부는 내부 기준을 몰라도 됨"]
    },
    {
      "ct_skill": "알고리즘적사고",
      "question": "cart가 위와 같을 때, '주문 합계'로 출력되는 값은?",
      "options": ["A. 10500", "B. 12000", "C. 13500", "D. 9000"],
      "answer": "B",
      "answer_type": "computational",
      "verification_snippet": "def total_price(cart):\n    total = 0\n    for item in cart:\n        total += item[\"price\"] * item[\"qty\"]\n    return total\n\nprint(total_price([{\"name\": \"공책\", \"price\": 3000, \"qty\": 2}, {\"name\": \"펜\", \"price\": 1500, \"qty\": 4}]))",
      "explanation": "공책 3000×2=6000, 펜 1500×4=6000, 합계 12000.",
      "focus_points": ["각 상품 가격×수량을 구해 더한다", "6000+6000=12000"]
    },
    {
      "ct_skill": "통합",
      "question": "이 프로그램 전체가 하는 일을 가장 잘 설명한 것은?",
      "options": [
        "A. 장바구니 상품들의 합계를 구하고 그 금액으로 주문 등급을 매긴다",
        "B. 상품을 가격순으로 정렬한다",
        "C. 가장 비싼 상품을 찾는다",
        "D. 상품 개수를 센다"
      ],
      "answer": "A",
      "answer_type": "conceptual",
      "verification_snippet": "",
      "explanation": "합계 계산(total_price)과 등급 판정(grade_order)이 결합해 하나의 주문 처리를 완성한다.",
      "focus_points": ["두 함수가 합계→등급으로 연결", "전체 목적은 합계+등급 산출"]
    }
  ]
}
```

---

## 6. LLM 프롬프트 템플릿 (Qwen3 8B)

> 규칙·스키마·예시는 **시스템 프롬프트**에 고정으로 넣고, 사용자 메시지로 파라미터만 바꾼다. Qwen3는 형식 지시에 민감하므로 **"JSON만 출력"**을 강조한다. `temperature`는 생성 0.6 권장.

### Stage 1 — 코드 생성 (system)
```
너는 중·고등학생의 '코드 읽기' 학습용 파이썬 예제를 만드는 출제자다.
규칙:
1. 외부 입력 없이 그대로 실행되는 단일 완결 프로그램 1개. 데이터는 코드 안에 고정, input() 금지.
2. 길이 최대 30줄(약 15~30줄), 함수 1~2개. 30줄을 넘기지 마라. 분해·통합 문항을 위해 함수 2개를 권장한다. 변수·함수 이름은 의미가 드러나게.
3. 코드에는 (a)구분되는 함수 또는 처리 단계 2개 이상, (b)반복문, (c)함수로 세부를 감춘 부분, (d)조건 분기, (e)부분이 합쳐져 하나의 목적을 이루는 구조가 모두 있어야 한다.
4. 아래 참고 예시의 구조·스타일만 본뜨고 그대로 복사하지 마라.
[참고 예시: {RAG: code_examples 검색 결과}]
5. 출력은 파이썬 코드 블록 하나만. 설명 문장 금지.
```
사용자 메시지: `난이도: {difficulty} / 주제 힌트: {topic_or_concept}`

### Stage 2 — MCQ 5문항 일괄 생성 (system)
```
아래 파이썬 코드를 읽고 4지선다 문제 5개를 만든다. 출력은 지정한 JSON 객체 1개만(코드블록 기호·설명 금지).
규칙:
1. 5문항의 ct_skill은 순서대로 분해, 패턴인식, 추상화, 알고리즘적사고, 통합. 각 1문항. 코드를 읽으면 풀 수 있어야 한다.
2. 각 문항 answer_type을 분류한다.
   - computational: 코드를 실행하면 값이 하나로 정해지는 문항 → verification_snippet 필수(정답 값 하나만 print하는 자족 실행 코드, input 금지).
   - conceptual: 의미·구조·목적을 묻는 문항 → verification_snippet은 "".
   computational 문항의 보기 값 형식은 verification_snippet의 출력과 정확히 일치시켜라.
3. 각 문항에 focus_points(채점·유도용 핵심 포인트 1~3개)를 넣는다. 정답을 그대로 적지 말고 '생각의 단서'로 적는다.
4. 정답은 보기 중 정확히 1개. 오답 보기도 그럴듯하게.
[유형별 출제 가이드: {RAG: problem_templates 검색 결과}]
[JSON 스키마와 예시: (4·5장 내용)]
```
사용자 메시지: `난이도: {difficulty}\n[파이썬 코드]\n{Stage1에서 생성된 code}`

---

## 7. 운영 체크리스트 (생성 직후)

- [ ] 코드가 외부 입력 없이 그대로 실행되는가 (2.2 다섯 구성요소 포함)
- [ ] 코드가 30줄 이하, 함수 1~2개인가
- [ ] 문항 5개, ct_skill 순서가 분해→통합인가
- [ ] computational 문항마다 verification_snippet이 있고, 실행 출력이 정답 보기와 일치하는가
- [ ] conceptual 문항의 verification_snippet이 ""인가
- [ ] 재생성 3회 실패 문항은 스킵 처리됐는가 (정답 덮어쓰기 ❌)
- [ ] 학생 전송 JSON에서 focus_points·verification_snippet·answer_type이 제거됐는가
