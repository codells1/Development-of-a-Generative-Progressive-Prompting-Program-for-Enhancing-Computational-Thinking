# 생성형 점진적 프롬프팅을 통한 컴퓨팅 사고력 향상 프로그램

캡스톤 프로젝트 | Flask 기반 Python 학습 보조 웹앱 | 제주도 중·고등학생 대상

---

## 프로그램 구조

Flask 웹앱. Python 학습 보조 도구.

### 전체 흐름
1. 사용자가 학습 단계(stage)에 진입하면 해당 주제의 Python 코드 예제 생성 (LM Studio 로컬 LLM 호출)
2. 그 코드를 기반으로 문제 5개를 하나씩 순차 생성 (CT 요소 순서 = 난이도 매우쉬움→매우어려움)
3. 학생이 답변을 제출하면 다음 문제 생성
4. 5문제 완료 시 컴퓨팅 사고력 + 프롬프팅 품질 자동 평가
5. 다음 단계로 진행. **4단계를 모두 마치면 마지막에 융합 종합 평가를 별도로 진행**

### LLM 연결
- LM Studio (localhost:1234/v1), OpenAI SDK 호환
- 채팅 모델: local-model (현재 Qwen3 8B)
- 임베딩 모델: text-embedding-bge-m3 (RAG용)

### RAG 구조
- 라이브러리: LangChain + FAISS
- rag_docs/code_examples/      ← 코드 예제 참고 TXT (주제별)
- rag_docs/problem_templates/  ← 문제 출제 템플릿 TXT (CT 요소별) **작성 완료**
- rag_db/                      ← FAISS 인덱스 저장 (자동 생성)
- 청크 크기: code_examples 450자, problem_templates 200자, 오버랩: 50자, TOP_K: 5

### 주요 파일
- app.py                       : Flask 앱 진입점
- api.py                       : 모든 API 엔드포인트 (Blueprint)
- rag.py                       : RAG 모듈 (FAISS 인덱싱 + 검색)
- static/main.js               : 프론트엔드 로직
- templates/index.html         : UI

---

## 핵심 설계 — 2차원 구조 (단계 × CT)

문제 출제는 **두 개의 독립적인 축**으로 구성된다.

### 축 1 — 학습 단계 (stage, 주제 진행) : 총 4단계
학생은 아래 4개 단계를 순서대로 진행한다. 단계가 곧 학습 커리큘럼이며, 각 단계는 좌측 패널에 제시할 코드의 주제를 결정한다.

| stage | 주제           | 비고          |
|-------|---------------|---------------|
| 0     | 순차/조건       | 가장 기초     |
| 1     | 반복문/리스트   |               |
| 2     | 함수           |               |
| 3     | 알고리즘        | 정렬·탐색     |

**융합은 단계에 포함되지 않는다.** (아래 "융합 종합 평가" 참조)

### 축 2 — CT 요소 (problem_index, 각 단계 내 5문제)
각 단계 안에서 5문제를 아래 순서로 출제한다. CT 요소 순서이자 난이도 순서다. 이 순서는 인지 부하 이론에 기반하며 변경하지 않는다.

| problem_index | CT 요소        | 난이도     | 질문 방향                    |
|---------------|----------------|------------|------------------------------|
| 0             | 분해           | 매우쉬움   | 코드를 역할별로 나눈다면?     |
| 1             | 패턴인식       | 쉬움       | 출력 패턴의 다음 값은?        |
| 2             | 추상화         | 보통       | 이 변수가 의미하는 것은?      |
| 3             | 알고리즘적사고 | 어려움     | n번 반복 후 변수 값은?        |
| 4             | 통합           | 매우어려움 | 이 코드가 해결하는 문제는?    |

### 두 축의 결합
```
stage=1, problem_index=2  →  "반복문/리스트" 주제 코드 + "추상화" 문제 (보통 난이도)
```
- 한 단계(stage)당 코드 1개를 생성하고, 그 코드로 5개의 CT 문제를 출제한다.
- 4단계 × 5문제 = 총 20문제가 정규 커리큘럼.
- CT 5종은 매 단계마다 반복되므로, 학생의 CT 성장을 단계 간 비교 측정할 수 있다.
- 코드 복잡도는 단계가 올라갈수록 자연스럽게 증가한다.

---

## 융합 종합 평가 (4단계 완료 후, 별도)

4개 단계를 모두 마친 뒤 마지막에 진행하는 **종합 평가 단계**다. 정규 학습 단계(stage)와는 분리되어 있다.

- 코드: 순차/조건 + 반복/리스트 + 함수 + 알고리즘을 **2개 이상 결합한 종합 코드** 제시
- 문제: 동일하게 CT 5문제(분해→통합) 출제
- 목적: 단일 주제가 아닌 통합적 상황에서 CT 발현을 종합 측정

### ⚠️ 이름 구분 (혼동 주의)
- **통합** = CT 요소 5번째. 한 코드의 전체 목적을 파악하는 사고력. 매 단계 problem_index=4 문제.
- **융합** = 주제(코드)의 한 종류. 여러 프로그래밍 개념을 결합한 종합 코드. 4단계 완료 후의 평가 단계.
- 둘은 서로 다른 축이다. 코드에서 변수·함수·파일명으로 섞이지 않도록 주의.

---

## RAG 검색 규칙 (중요)

두 RAG 폴더는 서로 다른 축으로 검색한다.

### code_examples ← stage(주제) 또는 융합으로 검색
좌측 패널 코드를 생성할 때 사용.
```python
# 정규 단계 (stage 0~3)
topic = STAGE_MAP[stage]              # 예: "반복문/리스트"
code_refs = rag_store.retrieve("code_examples", topic)

# 융합 종합 평가
code_refs = rag_store.retrieve("code_examples", "융합")
```

### problem_templates ← problem_index(CT 요소)로 검색  [작성 완료]
5개 문제를 생성할 때 사용. problem_index의 CT 요소로 검색한다.
```python
ct_skill   = CT_MAP[problem_index]["ct_skill"]      # 예: "추상화"
difficulty = CT_MAP[problem_index]["difficulty"]    # 예: "보통"
templates  = rag_store.retrieve("problem_templates", f"{ct_skill} {difficulty} {code[:300]}")
```
problem_templates/ 에는 CT 가이드 TXT(ct_분해.txt, ct_패턴인식.txt, ct_추상화.txt,
ct_알고리즘적사고.txt, ct_통합.txt)가 **이미 작성되어 있다.** 수정하지 말 것.

---

## 파일 구조

```
project/
├── app.py
├── api.py
├── rag.py
├── CLAUDE.md
│
├── rag_docs/
│   ├── problem_templates/        # ★ 작성 완료 — 수정 금지
│   │   ├── ct_분해.txt
│   │   ├── ct_패턴인식.txt
│   │   ├── ct_추상화.txt
│   │   ├── ct_알고리즘적사고.txt
│   │   └── ct_통합.txt
│   │
│   └── code_examples/            # ★ 작성 완료
│       ├── 순차조건.txt          # stage 0
│       ├── 반복리스트.txt        # stage 1
│       ├── 함수.txt              # stage 2
│       ├── 알고리즘.txt          # stage 3
│       └── 융합.txt              # 융합 종합 평가용 (단계 아님)
│
├── rag_db/                       # 자동 생성
├── static/main.js
└── templates/index.html
```

---

## 작업 목록

- [x] problem_templates CT 가이드 TXT 5개 작성 완료
- [x] Task 1: code_examples TXT 5개 생성 (4단계 + 융합)
- [x] Task 2: api.py 2차원 매핑(STAGE_MAP + CT_MAP + 융합 분기) 적용
- [x] Task 3: rag.py code_examples 청크 크기 분리 (450자)
- [x] Task 4: FAISS 재빌드 + 동작 확인

---

## 주의사항

1. **problem_templates/ 는 수정하지 말 것.** CT 가이드는 이미 완성됨.
2. **단계는 4개**(순차조건 / 반복리스트 / 함수 / 알고리즘). 융합은 단계가 아니라 4단계 완료 후의 별도 종합 평가.
3. **CT 5단계 순서**(분해→패턴인식→추상화→알고리즘적사고→통합)는 변경하지 않는다.
4. **단계 순서**(순차조건→반복리스트→함수→알고리즘)도 변경하지 않는다.
5. **통합(CT)과 융합(주제)을 혼동하지 말 것.** 서로 다른 축이다.
6. **rag.py 검색 로직**은 수정하지 않는다 (청크 크기와 쿼리 문자열만 변경 가능).
7. **LLM 호출 방식**(OpenAI SDK 호환, LM Studio)은 유지한다.
8. 소크라테스 챗봇이 있다면 정답을 직접 제공하지 않도록 유지한다.
