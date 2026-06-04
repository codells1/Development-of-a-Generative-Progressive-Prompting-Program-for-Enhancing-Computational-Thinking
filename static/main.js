const chatHistory = [];
const sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
let currentCode = null;
let allProblems = [];            // 코드 생성 직후 한 번에 받아 캐시된 5문항
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
    checkStatus();
    generateCode();
}

function setOverlay(visible, text = "") {
    const overlay = document.getElementById("loading-overlay");
    document.getElementById("loading-text").textContent = text;
    overlay.classList.toggle("hidden", !visible);
}

async function checkStatus() {
    const el = document.getElementById("lm-status");
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        if (data.connected) {
            el.textContent = "● 연결됨";
            el.style.color = "#38a169";
        } else {
            el.textContent = "● 연결 안 됨";
            el.style.color = "#e53e3e";
        }
    } catch {
        el.textContent = "● 연결 안 됨";
        el.style.color = "#e53e3e";
    }
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

        codeBlock.innerHTML = `<code>${escapeHtml(data.code)}</code>`;
        currentCode = data.code;
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
        body: JSON.stringify({ code: currentCode }),
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
    (options || []).forEach((opt, i) => {
        const label = String.fromCharCode(65 + i);
        const btn = document.createElement("button");
        btn.className = "option-btn";
        btn.dataset.value = label;
        btn.textContent = opt;
        btn.onclick = () => selectOption(label);
        container.appendChild(btn);
    });
}

function selectOption(label) {
    selectedOption = label;
    document.querySelectorAll(".option-btn").forEach(btn => {
        btn.classList.toggle("selected", btn.dataset.value === label);
    });
    document.getElementById("submit-btn").disabled = false;
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

    const ctSkill = data.ct_skill || "";
    document.getElementById("problem-count").textContent =
        `${currentProblemIndex + 1} / ${allProblems.length}`;
    problemDisplay.className = "text-display";
    problemDisplay.style.color = "";
    problemDisplay.innerHTML =
        `${ctSkill ? `<div class="ct-skill-badge">${escapeHtml(ctSkill)}</div>` : ""}` +
        `<div class="problem-item">${escapeHtml(data.question || "")}</div>`;

    renderOptions(data.options);
    document.getElementById("submit-btn").disabled = true;
    document.getElementById("feedback-area").classList.add("hidden");
    document.getElementById("answer-submit-row").classList.remove("hidden");
    answerArea.classList.add("visible");
    showHintButton();
}

function submitAnswer() {
    if (!selectedOption || !currentProblemData) return;

    const isCorrect = selectedOption === currentProblemData.answer;
    submittedAnswers.push(selectedOption);

    // 선택지 비활성화 + 정답/오답 색상
    document.querySelectorAll(".option-btn").forEach(btn => {
        btn.disabled = true;
        if (btn.dataset.value === currentProblemData.answer) {
            btn.classList.add("correct");
        } else if (btn.dataset.value === selectedOption && !isCorrect) {
            btn.classList.add("wrong");
        }
    });

    // 피드백 표시
    const badge = document.getElementById("feedback-badge");
    badge.className = "feedback-badge " + (isCorrect ? "feedback-correct" : "feedback-wrong");
    badge.textContent = isCorrect
        ? "✓ 정답"
        : `✗ 오답 — 정답: ${currentProblemData.answer}`;
    document.getElementById("feedback-explanation").textContent =
        currentProblemData.explanation || "";

    document.getElementById("answer-submit-row").classList.add("hidden");
    document.getElementById("feedback-area").classList.remove("hidden");

    // 힌트 없이 정답 → 챗봇이 풀이 설명 요청. 답글 받기 전까지 "다음 문제" 잠금.
    if (isCorrect && !hintUsed) {
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
    const topicEl = document.getElementById("eval-topic");
    topicEl.textContent = currentTopic;

    if (errorMsg || !data) {
        document.getElementById("ct-score").textContent = "?";
        document.getElementById("ct-feedback").textContent = errorMsg || "평가 중 오류가 발생했습니다.";
        document.getElementById("ct-score-ring").className = "score-ring";
    } else {
        const ctScore = parseInt(data.ct_score) || 0;
        document.getElementById("ct-score").textContent = ctScore;
        document.getElementById("ct-feedback").textContent = data.ct_feedback || "";
        document.getElementById("ct-score-ring").className = "score-ring " + scoreClass(ctScore);
    }

    document.querySelector(".eval-next-btn").textContent = "새 코드 →";

    overlay.classList.remove("hidden");
}

function scoreClass(score) {
    if (score >= 80) return "high";
    if (score >= 60) return "mid";
    return "low";
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
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
            }),
        });
        await streamSSE(res, (delta) => {
            fullReply += delta;
            bubble.textContent = fullReply;
            document.getElementById("chat-messages").scrollTop = 9999;
        });
        chatHistory.push({ role: "assistant", content: fullReply });
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
    input.disabled = true;
    appendBubble("user", message);
    chatHistory.push({ role: "user", content: message });

    // 사용자 답글이 들어왔으니 잠갔던 문제 풀이 섹터 즉시 해제
    if (awaitingFor === "hint") setAnswerInputEnabled(true);
    else if (awaitingFor === "explain") setNextProblemEnabled(true);
    awaitingFor = null;

    const bubble = appendBubble("assistant", "");
    let fullReply = "";
    try {
        const res = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                messages: chatHistory,
                session_id: sessionId,
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
            }),
        });
        await streamSSE(res, (delta) => {
            fullReply += delta;
            bubble.textContent = fullReply;
            document.getElementById("chat-messages").scrollTop = 9999;
        });
        chatHistory.push({ role: "assistant", content: fullReply });
    } catch (e) {
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
    document.getElementById("submit-btn").disabled = !enabled || !selectedOption;
}

function setNextProblemEnabled(enabled) {
    const btn = document.getElementById("next-problem-btn");
    if (btn) btn.disabled = !enabled;
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

init();
