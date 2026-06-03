"""
call_chatbot SYSTEM_CHAT 세 가지 시나리오 테스트
"""
import json
import requests

URL = "http://localhost:5000/api/chat"

# 공통 컨텍스트: 반복문이 5번 도는 점수 평균 코드
CODE = """\
scores = [85, 90, 78, 92, 88]
total = 0
for s in scores:
    total += s
average = total / len(scores)
print(average)
"""

PROBLEM = "이 코드를 실행하면 출력되는 값은 무엇인가요? A. 85  B. 86.6  C. 433  D. 90"

SCENARIOS = [
    {
        "label": "① 맞게 이해한 경우 (긍정 확인 + 심화 질문 기대)",
        "msg": "아, for문이 scores 리스트의 각 원소를 순서대로 돌면서 total에 더하는 거죠? 그래서 total이 점점 커지는 거고요.",
    },
    {
        "label": "② 틀리게 이해한 경우 (틀렸다는 신호 + 정답 없이 유도 기대)",
        "msg": "이 코드는 반복문이 3번 실행되는 것 같아요. scores 안에 숫자가 세 개 있으니까요.",
    },
    {
        "label": "③ 단순 질문 (소크라테스 유도 질문 기대)",
        "msg": "이 코드가 전체적으로 뭘 하는 건가요?",
    },
]


def read_sse(response) -> str:
    """SSE 스트림에서 delta를 모아 전체 텍스트로 반환."""
    parts = []
    for raw in response.iter_lines(decode_unicode=True):
        if not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "delta" in data:
            parts.append(data["delta"])
        elif data.get("done"):
            break
        elif "error" in data:
            parts.append(f"[ERROR] {data['error']}")
            break
    return "".join(parts)


def run_test(label: str, msg: str):
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"학생 입력: {msg}")
    print("-" * 60)

    body = {
        "session_id": "test",
        "messages": [{"role": "user", "content": msg}],
        "code_context": CODE,
        "current_problem": PROBLEM,
    }
    resp = requests.post(URL, json=body, stream=True, timeout=60)
    reply = read_sse(resp)
    print(f"챗봇 응답:\n{reply}")

    # 검증
    issues = []
    answer_keywords = ["86.6", "433", "B번", "A번", "C번", "D번", "정답은", "맞습니다"]
    for kw in answer_keywords:
        if kw in reply:
            issues.append(f"  ⚠ 정답값/라벨 직접 노출: '{kw}'")
    if not reply.strip().endswith("?") and "?" not in reply[-30:]:
        issues.append("  ⚠ 답변이 질문으로 끝나지 않음")

    if issues:
        print("\n[검증 실패]")
        for i in issues:
            print(i)
    else:
        print("\n[검증 통과] 정답값 미노출 + 질문으로 마무리")


if __name__ == "__main__":
    import sys
    out = open("test_chat_result.txt", "w", encoding="utf-8")
    sys.stdout = out
    for s in SCENARIOS:
        run_test(s["label"], s["msg"])
    print(f"\n{'='*60}")
    print("테스트 완료")
    out.close()
