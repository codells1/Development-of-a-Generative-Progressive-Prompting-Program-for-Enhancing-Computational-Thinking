const chatHistory = [];
let sessionId = newSessionId();   // 새 코드(=새 세션)마다 갱신 — 분석 세션 초기화

function newSessionId() {
    return `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
}
let currentCode = null;          // chat 컨텍스트용 — python_code를 가리킨다
let pythonCode = null;           // 코드 제시 자산(유형 3·4·5가 코드 섹션에 표시)
let pseudocodeLines = [];        // 코드 제시 자산(유형 1 블록 묶기의 입력)
let allProblems = [];            // 코드 생성 직후 한 번에 받아 캐시된 문항 세트
let currentProblemData = null;   // 현재 표시 중인 MCQ 객체
let currentProblemIndex = 0;
let submittedAnswers = [];       // 학생 선택 라벨 배열 (A/B/C/D)
let selectedOption = null;       // 현재 선택된 라벨
let currentTopic = "";
const TOTAL_PROBLEMS = 5;

// 챗봇 트리거 상태 (문제별 리셋)
let hintUsed = false;          // 이번 문제에서 힌트 버튼을 눌렀는가
let awaitingFor = null;        // "hint" | "explain" | null — 사용자 답글 대기 중인 트리거 종류

function init() {
    generateCode();
}

function setOverlay(visible, text = "") {
    const overlay = document.getElementById("loading-overlay");
    document.getElementById("loading-text").textContent = text;
    overlay.classList.toggle("hidden", !visible);
}

async function generateCode() {
    const codeBlock = document.getElementById("code-display");
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");

    currentProblemIndex = 0;
    allProblems = [];
    submittedAnswers = [];
    currentProblemData = null;
    selectedOption = null;
    currentTopic = "";
    hideHintButton();

    // 새 코드 = 새 세션. 챗봇 대화·기록과 분석 세션(session_id)을 초기화한다.
    sessionId = newSessionId();
    chatHistory.length = 0;
    awaitingFor = null;
    hintUsed = false;
    document.getElementById("chat-messages").innerHTML = "";

    setOverlay(true, "코드 만드는 중...");
    codeBlock.innerHTML = "<code></code>";
    problemDisplay.className = "text-display placeholder";
    problemDisplay.textContent = "";
    document.getElementById("problem-count").textContent = "";
    answerArea.classList.remove("visible");

    try {
        const res = await fetch("/api/generate-code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        });
        const data = await res.json();

        if (data.error) {
            codeBlock.innerHTML = `<code style="color:#e53e3e">${data.error}</code>`;
            return;
        }

        // 코드 패널은 문항 전환 시 code_kind에 따라 그린다(여기서 미리 그리지 않음).
        pythonCode = data.python_code || data.code || "";
        pseudocodeLines = data.pseudocode_lines || [];
        currentCode = pythonCode;
        currentTopic = data.topic || "";

        setOverlay(true, `문제 ${TOTAL_PROBLEMS}개 만드는 중...`);
        await loadAllProblems();
        showCurrentProblem();
    } catch (e) {
        codeBlock.innerHTML = `<code style="color:#e53e3e">오류: ${e.message}</code>`;
    } finally {
        setOverlay(false);
    }
}

async function loadAllProblems() {
    const res = await fetch("/api/generate-problem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            python_code: pythonCode,
            pseudocode_lines: pseudocodeLines,
            session_id: sessionId,
        }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    allProblems = data.questions || [];
    // 검증 실패로 스킵된 문항이 있을 수 있으므로 최소 1개만 보장
    if (allProblems.length === 0) {
        throw new Error("생성된 문제가 없습니다.");
    }
}

function renderOptions(options) {
    const container = document.getElementById("options-list");
    container.innerHTML = "";
    container.classList.remove("diagram-grid");
    const isDiagram = currentProblemData && currentProblemData.type === "diagram";
    (options || []).forEach((opt, i) => {
        const label = String.fromCharCode(65 + i);
        const btn = document.createElement("button");
        btn.className = "option-btn" + (isDiagram ? " option-diagram-pick" : "");
        btn.dataset.value = label;
        if (isDiagram) {
            // 유형 2: 인라인 순서도(긴 스크롤) 대신 압축 버튼. 클릭하면 별도 창에 크게 표시.
            btn.dataset.def = opt;
            btn.textContent = `${label}. 순서도 보기 ▸`;
            btn.onclick = () => openDiagramModal(label, opt);
        } else {
            // 그 외: 순수 값/순서 문자열 → 화면에 A./B. 라벨을 붙여 보여준다.
            btn.textContent = `${label}. ${opt}`;
            btn.onclick = () => selectOption(label);
        }
        container.appendChild(btn);
    });
}

// Mermaid 정의 문자열을 SVG로 렌더해 box에 넣는다. 실패(오프라인 등) 시 원본 텍스트로 폴백.
async function renderMermaidInto(box, definition, id) {
    try {
        if (!window.mermaid) { box.textContent = definition; return; }
        const { svg } = await mermaid.render(`${id}-${Date.now()}`, definition);
        box.innerHTML = svg;
    } catch (e) {
        box.innerHTML = `<pre class="diagram-fallback">${escapeHtml(definition)}</pre>`;
    }
}

// ── 유형 2 순서도 보기 모달 ──────────────────────────────────────────
let modalLabel = null;   // 모달에 현재 표시 중인 보기 라벨

function openDiagramModal(label, definition) {
    modalLabel = label;
    document.getElementById("diagram-modal-title").textContent = `보기 ${label}`;
    const body = document.getElementById("diagram-modal-body");
    body.innerHTML = "";
    const box = document.createElement("div");
    box.className = "diagram-box-modal";
    box.textContent = "순서도 그리는 중...";
    body.appendChild(box);
    renderMermaidInto(box, definition, `mmd-modal-${currentProblemIndex}-${label}`);

    const selBtn = document.getElementById("diagram-select-btn");
    selBtn.textContent = (selectedOption === label) ? "✓ 선택된 보기" : "이 순서도를 정답으로 선택";
    document.getElementById("diagram-overlay").classList.remove("hidden");
}

function selectFromModal() {
    if (modalLabel) selectOption(modalLabel);   // 기존 채점·하이라이트 로직 그대로 사용
    closeDiagramModal();
}

function closeDiagramModal() {
    document.getElementById("diagram-overlay").classList.add("hidden");
}

// 코드 섹션을 현재 문항에 맞춰 그린다.
//  - 유형 1(order): 섞인 의사코드 블록을 라벨과 함께 표시(학생이 순서를 맞춘다).
//  - 유형 2(diagram) 등 code_kind=pseudocode: 의사코드를 텍스트로 표시.
//  - 그 외(python): 파이썬 코드를 그대로 표시.
function renderCodePanel(q) {
    const codeBlock = document.getElementById("code-display");
    if (q && q.type === "order" && Array.isArray(q.blocks)) {
        const html = q.blocks.map(b =>
            `<div class="pseudo-block">` +
            `<span class="blk-label">${escapeHtml(b.label || "")}</span>` +
            `<span class="blk-lines">${escapeHtml((b.lines || []).join("\n"))}</span>` +
            `</div>`
        ).join("");
        codeBlock.innerHTML = `<div class="pseudo-blocks">${html}</div>`;
    } else if (q && q.code_kind === "pseudocode") {
        codeBlock.innerHTML = `<code>${escapeHtml((pseudocodeLines || []).join("\n"))}</code>`;
    } else {
        codeBlock.innerHTML = `<code>${escapeHtml(pythonCode || currentCode || "")}</code>`;
    }
}

function selectOption(label) {
    selectedOption = label;
    document.querySelectorAll(".option-btn").forEach(btn => {
        btn.classList.toggle("selected", btn.dataset.value === label);
    });
    setSubmitVisible(true);   // 보기를 골랐으니 제출 버튼 노출·활성
}

function showCurrentProblem() {
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");

    hintUsed = false;
    awaitingFor = null;
    hideHintButton();

    const data = allProblems[currentProblemIndex];
    if (!data) {
        problemDisplay.className = "text-display";
        problemDisplay.style.color = "#e53e3e";
        problemDisplay.textContent = `문제 ${currentProblemIndex + 1}을(를) 찾을 수 없습니다.`;
        return;
    }

    currentProblemData = data;
    selectedOption = null;

    renderCodePanel(data);   // 문항 유형(code_kind)에 맞게 코드/의사코드 패널 갱신

    const ctSkill = data.ct_skill || "";
    document.getElementById("problem-count").textContent =
        `${currentProblemIndex + 1} / ${allProblems.length}`;
    problemDisplay.className = "text-display";
    problemDisplay.style.color = "";
    problemDisplay.innerHTML =
        `${ctSkill ? `<div class="ct-skill-badge">${escapeHtml(ctSkill)}</div>` : ""}` +
        `<div class="problem-item">${escapeHtml(data.question || "")}</div>`;

    renderOptions(data.options);
    setSubmitVisible(false);   // 아직 안 골랐으니 제출 버튼 숨김
    document.getElementById("feedback-area").classList.add("hidden");
    // 새 문제는 아직 안 풀었으므로 '다음 문제' 버튼을 확실히 숨기고 상태를 초기화한다.
    // (정답을 맞혀야만 submitAnswer에서 hidden을 풀어 노출 → 오답·미응답 시엔 절대 안 보임)
    const nextBtn = document.getElementById("next-problem-btn");
    nextBtn.classList.add("hidden");
    nextBtn.disabled = false;
    document.getElementById("answer-submit-row").classList.remove("hidden");
    answerArea.classList.add("visible");
    showHintButton();
}

function submitAnswer() {
    if (!selectedOption || !currentProblemData) return;

    const isCorrect = selectedOption === currentProblemData.answer;
    const badge = document.getElementById("feedback-badge");
    const feedbackArea = document.getElementById("feedback-area");
    const nextBtn = document.getElementById("next-problem-btn");

    if (!isCorrect) {
        // 정답은 절대 알려주지 않는다. 고른 보기만 '틀림'으로 표시·잠그고 다시 풀게 한다.
        // 정답을 맞히기 전까지 다음 문제로 넘어갈 수 없다.
        const picked = selectedOption;
        document.querySelectorAll(".option-btn").forEach(btn => {
            if (btn.dataset.value === picked) {
                btn.classList.add("wrong");
                btn.disabled = true;        // 같은 오답 재선택 방지
            }
        });

        badge.className = "feedback-badge feedback-wrong";
        badge.textContent = "✗ 틀렸어요. 다른 보기를 다시 골라 보세요.";
        document.getElementById("feedback-explanation").textContent = "";   // 해설=정답 누설이라 숨김
        nextBtn.classList.add("hidden");                                    // 다음 문제 잠금
        document.getElementById("answer-submit-row").classList.remove("hidden");
        feedbackArea.classList.remove("hidden");

        // 선택 초기화 → 새 보기를 고르기 전까지 제출 버튼을 숨긴다(헷갈려 누르는 것 방지)
        selectedOption = null;
        document.querySelectorAll(".option-btn").forEach(b => b.classList.remove("selected"));
        setSubmitVisible(false);
        return;
    }

    // ── 정답 ──
    submittedAnswers.push(selectedOption);   // 정답일 때만 1회 기록 (문항당 정확히 1개)
    document.querySelectorAll(".option-btn").forEach(btn => {
        btn.disabled = true;
        if (btn.dataset.value === currentProblemData.answer) {
            btn.classList.add("correct");
        }
    });

    badge.className = "feedback-badge feedback-correct";
    badge.textContent = "✓ 정답";
    document.getElementById("feedback-explanation").textContent =
        currentProblemData.explanation || "";
    nextBtn.classList.remove("hidden");

    document.getElementById("answer-submit-row").classList.add("hidden");
    feedbackArea.classList.remove("hidden");

    // 힌트 없이 정답 → 챗봇이 풀이 설명 요청. 답글 받기 전까지 "다음 문제" 잠금.
    if (!hintUsed) {
        awaitingFor = "explain";
        setNextProblemEnabled(false);
        hideHintButton();
        triggerChatbot("explain");
    }
}

async function nextProblem() {
    currentProblemIndex++;

    if (currentProblemIndex >= allProblems.length) {
        const problemDisplay = document.getElementById("problem-display");
        const answerArea = document.getElementById("answer-area");
        document.getElementById("problem-count").textContent = "완료";
        problemDisplay.className = "text-display";
        problemDisplay.innerHTML = `<div class="complete-msg">모든 문제를 완료했습니다! 평가 중...</div>`;
        answerArea.classList.remove("visible");
        await triggerEvaluation();
        return;
    }

    showCurrentProblem();
}

async function triggerEvaluation() {
    setOverlay(true, "학습 결과 평가 중...");
    try {
        const res = await fetch("/api/evaluate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                session_id: sessionId,
                topic: currentTopic,
                code: currentCode,
                problems: allProblems.map(p => p.question),
                answers: submittedAnswers,
                chat_history: chatHistory,
            }),
        });
        const data = await res.json();
        if (data.error) {
            showEvalResult(null, data.error);
        } else {
            showEvalResult(data);
        }
    } catch (e) {
        showEvalResult(null, e.message);
    } finally {
        setOverlay(false);
    }
}

function showEvalResult(data, errorMsg = null) {
    const overlay = document.getElementById("eval-overlay");
    document.getElementById("eval-topic").textContent = currentTopic;

    // 결과 화면은 '평가 근거' 중심. (총평·루브릭 지표 점수는 화면에서 제거)
    if (errorMsg || !data) {
        renderEvidence(null, errorMsg || "평가 중 오류가 발생했습니다.");
    } else {
        renderEvidence(data.highlights);
    }

    document.querySelector(".eval-next-btn").textContent = "새 코드 →";
    overlay.classList.remove("hidden");
}

// 평가 근거: 학생의 실제 발화 → 어느 요소에서 어떤 등급으로, 왜 그렇게 평가됐는지.
// "내 대화의 어떤 부분이 어떻게 평가됐는지"를 학생이 직접 확인하게 한다.
function renderEvidence(highlights, emptyMsg = null) {
    const box = document.getElementById("ct-evidence");
    box.innerHTML = "";
    if (!Array.isArray(highlights) || highlights.length === 0) {
        const msg = emptyMsg ||
            "이번 세션에서는 평가 근거로 삼을 만한 대화가 충분하지 않았어요. " +
            "다음엔 챗봇과 더 이야기하며 질문해 보면, 어떤 말이 어떻게 평가됐는지 여기서 확인할 수 있어요.";
        const p = document.createElement("p");
        p.className = "evidence-empty";
        p.textContent = msg;
        box.appendChild(p);
        return;
    }
    highlights.forEach((h) => box.appendChild(evidenceItem(h)));
}

function evidenceItem(h) {
    const grade = h.grade;
    const cls = { "상": "g-sang", "중": "g-jung", "하": "g-ha" }[grade] || "g-na";

    const item = document.createElement("div");
    item.className = "evidence-item";

    // 상단: 요소 배지 + 등급 칩
    const head = document.createElement("div");
    head.className = "evidence-head";
    const tag = document.createElement("span");
    tag.className = "evidence-element";
    tag.textContent = h.element || "";
    const chip = document.createElement("span");
    chip.className = "rubric-chip " + cls;
    chip.textContent = grade || "—";
    head.appendChild(tag);
    head.appendChild(chip);

    // 학생 발화 인용
    const quote = document.createElement("blockquote");
    quote.className = "evidence-quote";
    quote.textContent = h.quote || "";

    item.appendChild(head);
    item.appendChild(quote);

    // 왜 그렇게 평가됐는지 (있을 때만)
    if (h.reason) {
        const reason = document.createElement("p");
        reason.className = "evidence-reason";
        reason.textContent = h.reason;
        item.appendChild(reason);
    }
    return item;
}

function closeEvalAndRestart() {
    document.getElementById("eval-overlay").classList.add("hidden");
    generateCode();
}

// ── 챗봇 트리거 / 메시지 / 헬퍼 ─────────────────────────────────────

async function requestHint() {
    if (!currentProblemData || awaitingFor || hintUsed) return;
    hintUsed = true;
    awaitingFor = "hint";
    hideHintButton();
    setAnswerInputEnabled(false);
    await triggerChatbot("hint");
}

function showHintButton() {
    hideHintButton();
    const container = document.getElementById("chat-messages");
    const btn = document.createElement("button");
    btn.id = "hint-btn";
    btn.className = "hint-btn hint-btn-inline";
    btn.textContent = "💡 힌트";
    btn.onclick = requestHint;
    container.appendChild(btn);
    container.scrollTop = container.scrollHeight;
}

function hideHintButton() {
    const btn = document.getElementById("hint-btn");
    if (btn) btn.remove();
}

async function triggerChatbot(triggerType) {
    const input = document.getElementById("chat-input");
    input.disabled = true;
    const bubble = appendBubble("assistant", "");
    let fullReply = "";
    try {
        const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                trigger: triggerType,
                session_id: sessionId,
                problem_index: currentProblemIndex,
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
            }),
        });
        await streamSSE(res, (delta) => {
            fullReply += delta;
            bubble.textContent = fullReply;
            document.getElementById("chat-messages").scrollTop = 9999;
        });
        fullReply = cleanReply(fullReply);
        if (fullReply) {
            bubble.textContent = fullReply;
            chatHistory.push({ role: "assistant", content: fullReply });
        } else {
            bubble.remove();   // 빈 응답이면 빈 말풍선을 남기지 않는다
        }
    } catch (e) {
        bubble.className = "chat-bubble error";
        bubble.textContent = `오류: ${e.message}`;
        // 트리거 실패 시 사용자가 막히지 않도록 풀이 섹터 복구
        if (awaitingFor === "hint") setAnswerInputEnabled(true);
        else if (awaitingFor === "explain") setNextProblemEnabled(true);
        awaitingFor = null;
    } finally {
        input.disabled = false;
        input.focus();
    }
}

async function sendMessage() {
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    appendBubble("user", message);
    chatHistory.push({ role: "user", content: message });

    // 사용자 답글이 들어왔으니 잠갔던 문제 풀이 섹터 즉시 해제
    const mode = awaitingFor;   // "hint" | "explain" | null
    if (awaitingFor === "hint") setAnswerInputEnabled(true);
    else if (awaitingFor === "explain") setNextProblemEnabled(true);
    awaitingFor = null;

    // 힌트 없이 정답 → '어떻게 풀었어?'에 대한 학생 답변.
    // 이 답변은 기록만 하고 챗봇은 추가로 응답하지 않는다(대화 종료).
    if (mode === "explain") {
        input.focus();
        return;
    }

    // 힌트에 대한 학생의 생각 → 정답 방향이면 응답 중지, 오해면 한 번 더 유도.
    if (mode === "hint") {
        await handleHintFollowup();
        return;
    }

    input.disabled = true;
    const bubble = appendBubble("assistant", "");
    let fullReply = "";
    try {
        const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                messages: chatHistory,
                session_id: sessionId,
                problem_index: currentProblemIndex,
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
            }),
        });
        await streamSSE(res, (delta) => {
            fullReply += delta;
            bubble.textContent = fullReply;
            document.getElementById("chat-messages").scrollTop = 9999;
        });
        fullReply = cleanReply(fullReply);
        if (fullReply) {
            bubble.textContent = fullReply;
            chatHistory.push({ role: "assistant", content: fullReply });
        } else {
            bubble.remove();   // 빈 응답이면 빈 말풍선을 남기지 않는다
        }
    } catch (e) {
        bubble.className = "chat-bubble error";
        bubble.textContent = `오류: ${e.message}`;
    } finally {
        input.disabled = false;
        input.focus();
    }
}

// 힌트 후 학생 발화를 서버에서 판정.
//   on_track  → 정답 방향이 맞음 → 챗봇 응답 없이 종료(설명 흐름과 동일).
//   !on_track → 오해·오답 → 유도 질문을 한 번 더 띄우고 대화를 이어간다(다음 답변도 다시 판정).
async function handleHintFollowup() {
    const input = document.getElementById("chat-input");
    input.disabled = true;
    try {
        const res = await fetch("/api/hint-followup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                messages: chatHistory,
                session_id: sessionId,
                problem_index: currentProblemIndex,
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
            }),
        });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        if (data.on_track) {
            return;   // 정답 방향 → 추가 응답 없음(대화 종료)
        }
        const reply = cleanReply(data.reply || "");
        if (reply) {
            appendBubble("assistant", reply);
            chatHistory.push({ role: "assistant", content: reply });
            awaitingFor = "hint";   // 다음 학생 답변도 같은 방식으로 판정 — 맞을 때까지 이어감
        }
    } catch (e) {
        const bubble = appendBubble("assistant", "");
        bubble.className = "chat-bubble error";
        bubble.textContent = `오류: ${e.message}`;
    } finally {
        input.disabled = false;
        input.focus();
    }
}

async function streamSSE(res, onDelta) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const lines = decoder.decode(value).split("\n");
        for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const payload = JSON.parse(line.slice(6));
            if (payload.error) throw new Error(payload.error);
            if (payload.delta) onDelta(payload.delta);
        }
    }
}

function appendBubble(role, text) {
    const container = document.getElementById("chat-messages");
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    bubble.textContent = text;
    const hintBtn = document.getElementById("hint-btn");
    if (hintBtn) {
        container.insertBefore(bubble, hintBtn);  // 힌트 버튼은 항상 최하단에 유지
    } else {
        container.appendChild(bubble);
    }
    container.scrollTop = container.scrollHeight;
    return bubble;
}

function setAnswerInputEnabled(enabled) {
    document.querySelectorAll(".option-btn").forEach(btn => {
        if (!btn.classList.contains("correct") && !btn.classList.contains("wrong")) {
            btn.disabled = !enabled;
        }
    });
    setSubmitVisible(enabled && !!selectedOption);   // 고른 보기가 있을 때만 제출 노출
}

function setNextProblemEnabled(enabled) {
    const btn = document.getElementById("next-problem-btn");
    if (btn) btn.disabled = !enabled;
}

// 제출 버튼은 '고른 보기가 있을 때'만 보이고 동작한다.
// 새 문제·오답 직후엔 선택이 비므로 숨겨지고, 보기를 (다시) 고르면 다시 나타난다.
function setSubmitVisible(visible) {
    const btn = document.getElementById("submit-btn");
    btn.classList.toggle("hidden", !visible);
    btn.disabled = !visible;
}

// 모델이 답변 전체를 따옴표로 감싼 경우 양끝 따옴표를 제거하고 공백을 정리한다.
function cleanReply(text) {
    let t = (text || "").trim();
    if (t.length >= 2) {
        const a = t[0], b = t[t.length - 1];
        const pairs = [['"', '"'], ["'", "'"], ["“", "”"], ["‘", "’"]];
        if (pairs.some(([o, c]) => a === o && b === c)) {
            t = t.slice(1, -1).trim();
        }
    }
    return t;
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

init();
