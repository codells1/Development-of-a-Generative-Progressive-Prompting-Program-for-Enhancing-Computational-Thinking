# 문항(MCQ) 생성 사양 (개정 v2) — 5유형, 전부 4지선다

> **적용 범위:** `code_presentation_revision.md`가 만든 자산(`python_code`, `pseudocode_lines`)을
> 입력으로 5유형 문항을 생성. 기존 Stage 2(문항 생성)를 대체.
>
> **모든 유형은 4지선다(객관식)다.** 공통 형태: 보기 4개(`options`) 중 정답 1개(`answer_index`).
> 유형마다 "보기 한 개"의 *내용물*만 다르다 — 순서 문자열 / 순서도 / 값.

---

## 0. 두 갈래 생성 (검증 가능성 기준)

- **경로 A — 결정적 (LLM·RAG 없음): 유형 1, 2.**
  순서·순서도는 실행으로 정답을 검증할 수 없으므로, 보기·정답을 **코드로 구성**한다(정답 보장).
  객관식이어도 이 점은 동일하다 — 보기 4개를 코드가 만든다.
- **경로 B — LLM 생성 + 실행 검증 (problem_templates RAG): 유형 3, 4, 5.**
  계산형. `verification_snippet`을 실행해 정답을 확정한다.

---

## 1. 유형 1 — 문제분해  (경로 A · 객관식 "순서 고르기")

- 입력: `pseudocode_lines` (정답 순서).
- **블록 묶기:** 최상위 단계 + 그 들여쓰기 하위 = 한 블록(결정적). 블록에 라벨 `(가)(나)(다)(라)…` 부여.
- **화면:** 블록을 *섞은 순서로* 라벨과 함께 코드 섹션에 표시.
- **보기(4개):** 라벨 순서 문자열. 정답 1개 = 원래 순서, 오답 3개 = 코드로 만든 다른 순열(인접 블록 교환, 한 블록 이동 등; 서로 다르고 정답과도 다름).
- 발문 고정: "위 단계들을 올바른 순서로 나열한 것은?"
- LLM 없음. 구조 검증: 블록 ≥ 3, 보기 4개가 서로 다른 순열, 정답 정확히 1개.
- 객체: `{ ct_skill:"문제분해", code_kind:"pseudocode", blocks:[{label,lines}…(섞인 순서)], stem, options:["(가)-(다)-(나)-(라)", …], answer_index, answer_type:"order" }`

## 2. 유형 2 — 추상화/알고리즘  (경로 A · 객관식 "순서도 고르기")

- 입력: `python_code` (또는 pseudocode_lines).
- **정답 순서도:** AST → 구조 판별(반복만 / 반복+조건) → 골격에 실제 조건식·갱신문 채움 → Mermaid.
- **보기(4개):** Mermaid 순서도. 정답 1개 + 오답 3개 = 정답 그래프의 *통제된 변형*(조건 yes/no 뒤바꾸기, 반복 복귀 화살표 제거, 초기화·반복 순서 뒤바꾸기). 프론트가 각 보기를 순서도로 렌더.
- 발문 고정: "위 의사코드를 올바르게 나타낸 순서도는?"
- LLM 없음. 구조 검증: 정답 그래프가 코드 구조와 일치, 보기 4개 서로 다름.
- 객체: `{ ct_skill:"추상화/알고리즘", code_kind:"pseudocode", stem, options:[mermaid×4], answer_index, answer_type:"diagram" }`

## 3. 유형 3 — 패턴인식  (경로 B · 객관식)

- 입력: `python_code`. **변수 식별(AST):** 반복 변수(예: `i`)와 누적 변수(예: `total`).
- 발문(템플릿): "다음 코드에서 {반복변수}가 {대상값}일 때 {누적변수}의 값은?"
- **보기(4개):** 정답 = 그 시점 누적값(`verification_snippet` 실행값). 오답 = {대상값}±1 시점 값, 반복변수 값 자체, 최종 누적값 등(실행 변형으로 생성).
- `answer_type:"computational"`. 보기 값은 snippet 출력과 **문자 단위 일치**(단위·접미사 금지).

## 4. 유형 4 — 코드: 중간 출력  (경로 B · 객관식)

> **⚠ 전제 의존성:** 코드가 *실행 중 여러 줄을 출력*할 때만 성립. 최종 `print` 1개뿐인 코드엔 "중간 출력"이
> 없다. → **code_presentation_revision.md §3에 "반복문 안에서 진행 상황 print" 요건 추가** 필요(카운트다운류).
> 그러면 한 프로그램에서 유형3·4·5가 모두 성립.

- 체크포인트 *정확히 정의*("N번째 반복까지", "반환 직전까지").
- **보기(4개):** 정답 = 체크포인트까지 stdout(`verification_snippet` 실행값). 오답 = 한 줄 누락/추가, 한 번 더/덜 반복, 최종 출력 전체 등.
- `answer_type:"computational"`.

## 5. 유형 5 — 코드: 최종 출력  (경로 B · 객관식)

- 발문 고정: "위 코드를 실행하면 최종 출력은?"
- **보기(4개):** 정답 = 전체 실행 stdout(`verification_snippet` = 코드 자체). 오답 = 중간값을 최종값으로 착각, off-by-one, 형식 오류(따옴표·콤마·소수점) 등.
- `answer_type:"computational"`.

---

## 6. 검증·재생성

- **경로 B(유형 3·4·5):** `verification_snippet`을 subprocess(5초) 실행, 정답 보기가 출력과 문자 단위 일치 확인, 불일치 시 **최대 3회 재생성(보기 덮어쓰기 금지)**. 모든 LLM 호출 `response_format=json_schema`(재생성 경로 포함).
- **경로 A(유형 1·2):** LLM 없음 → 재생성 없음. 구조 검증만 — 유형1: 블록≥3·보기 4개 서로 다른 순열·정답 1개; 유형2: 정답 그래프가 코드 구조와 일치·보기 4개 서로 다름.

## 7. 출력 스키마 (공통 — 전부 4지선다)

```json
{
  "ct_skill": "...",
  "code_kind": "pseudocode | python",
  "stem": "...",
  "options": ["...", "...", "...", "..."],
  "answer_index": 0,
  "answer_type": "order | diagram | computational",
  "verification_snippet": "",
  "focus_points": ["..."]
}
```

- `options`는 항상 4개, `answer_index`는 정답 1개. *내용물*만 유형마다 다름 — 유형1=순서 문자열, 유형2=Mermaid 순서도, 유형3·4·5=값(텍스트/숫자).
- 유형1은 추가로 `blocks`(라벨+줄, 섞인 순서)를 둬서 화면에 표시.
- `verification_snippet`은 경로 B만 채우고 경로 A는 `""`.
- 학생 전송 JSON에서 `verification_snippet`·`focus_points`·`answer_type` 제거.

## 8. RAG

- `problem_templates`: **유형 3·4·5만** 사용. **유형 1·2는 RAG 불필요**(보기·정답을 코드가 생성).
- `code_examples`는 코드 생성용 — 문항 생성에는 쓰지 않음.
