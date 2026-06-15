# -*- coding: utf-8 -*-
"""출력 0줄짜리 코드가 어떻게 생기는지 재현해 실물을 본다."""
import sys, json
sys.stdout.reconfigure(encoding="utf-8")
import api, llm

for i in range(10):
    topic = "단어·문자열 분석"
    ctx = api.rag_store.retrieve("code_examples", topic)
    try:
        raw = llm.call_code_gen(topic=topic, ctx=ctx, difficulty=api.TARGET_DIFFICULTY)
        code = api._clean_code(api.strip_fences(
            json.loads(api._slice_first_json(raw), strict=False).get("python_code", "")))
    except Exception as e:
        print(f"[{i}] 파싱실패 {e}"); continue
    out = api._run_code_stdout(code)
    nlines = len((out or "").splitlines())
    print(f"\n[{i}] 출력 {nlines}줄")
    if nlines < 2:
        print("---- 출력 부족 코드 ----")
        print(code)
        print("---- 끝 ----")
        break
