const chatHistory = [];
const sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
let currentCode = null;
let currentProblem = null;
let currentProblemIndex = 0;
let previousProblems = [];
let submittedAnswers = [];
let currentTopic = "";
const TOTAL_PROBLEMS = 5;

const TOPICS = [
    "변수와 자료형",
    "입력과 출력",
    "조건문",
    "for 반복문",
    "while 반복문",
    "함수",
    "리스트 조작",
    "문자열 조작",
    "딕셔너리",
    "재귀함수",
    "예외처리",
];
let currentTopicIndex = 0;

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
    const topic = TOPICS[currentTopicIndex];
    currentTopicIndex = (currentTopicIndex + 1) % TOPICS.length;
    currentTopic = topic;

    const codeBlock = document.getElementById("code-display");
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");

    currentProblemIndex = 0;
    previousProblems = [];
    submittedAnswers = [];
    currentProblem = null;

    document.getElementById("topic-label").textContent = topic;
    setOverlay(true, `${topic} 코드 만드는 중...`);
    codeBlock.innerHTML = "<code></code>";
    problemDisplay.className = "text-display placeholder";
    problemDisplay.textContent = "";
    document.getElementById("problem-count").textContent = "";
    answerArea.classList.remove("visible");

    try {
        const res = await fetch("/api/generate-code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ topic }),
        });
        const data = await res.json();

        if (data.error) {
            codeBlock.innerHTML = `<code style="color:#e53e3e">${data.error}</code>`;
            return;
        }

        codeBlock.innerHTML = `<code>${escapeHtml(data.code)}</code>`;
        currentCode = data.code;

        setOverlay(true, `문제 1 / ${TOTAL_PROBLEMS} 만드는 중...`);
        await generateNextProblem();
    } catch (e) {
        codeBlock.innerHTML = `<code style="color:#e53e3e">오류: ${e.message}</code>`;
    } finally {
        setOverlay(false);
    }
}

async function generateNextProblem() {
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");
    const answerInput = document.getElementById("answer-input");

    try {
        const res = await fetch("/api/generate-problem", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                code: currentCode,
                problem_index: currentProblemIndex,
                previous_problems: previousProblems,
            }),
        });
        const data = await res.json();

        if (data.error) {
            problemDisplay.className = "text-display";
            problemDisplay.style.color = "#e53e3e";
            problemDisplay.textContent = `오류: ${data.error}`;
            return;
        }

        const problem = (data.problem || "").trim();
        if (!problem) {
            problemDisplay.className = "text-display";
            problemDisplay.style.color = "#e53e3e";
            problemDisplay.textContent = "문제를 생성하지 못했습니다.";
            return;
        }

        previousProblems.push(problem);
        currentProblem = problem;

        const ctSkill = data.ct_skill || "";
        document.getElementById("problem-count").textContent =
            `${currentProblemIndex + 1} / ${TOTAL_PROBLEMS}`;
        problemDisplay.className = "text-display";
        problemDisplay.style.color = "";
        problemDisplay.innerHTML =
            `${ctSkill ? `<div class="ct-skill-badge">${escapeHtml(ctSkill)}</div>` : ""}` +
            `<div class="problem-item">${escapeHtml(problem)}</div>`;

        answerInput.value = "";
        answerArea.classList.add("visible");
        answerInput.focus();
    } catch (e) {
        console.error("[문제생성] 오류:", e);
        problemDisplay.className = "text-display";
        problemDisplay.style.color = "#e53e3e";
        problemDisplay.textContent = `문제 생성 오류: ${e.message}`;
    }
}

async function submitAnswer() {
    const answerInput = document.getElementById("answer-input");
    const answer = answerInput.value.trim();
    if (!answer) return;

    submittedAnswers.push(answer);
    currentProblemIndex++;

    if (currentProblemIndex >= TOTAL_PROBLEMS) {
        const problemDisplay = document.getElementById("problem-display");
        const answerArea = document.getElementById("answer-area");
        document.getElementById("problem-count").textContent = "완료";
        problemDisplay.className = "text-display";
        problemDisplay.innerHTML = `<div class="complete-msg">모든 문제를 완료했습니다! 평가 중...</div>`;
        answerArea.classList.remove("visible");
        await triggerEvaluation();
        return;
    }

    setOverlay(true, `문제 ${currentProblemIndex + 1} / ${TOTAL_PROBLEMS} 만드는 중...`);
    try {
        await generateNextProblem();
    } finally {
        setOverlay(false);
    }
}

async function triggerEvaluation() {
    setOverlay(true, "학습 결과 평가 중...");
    try {
        const res = await fetch("/api/evaluate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                topic: currentTopic,
                code: currentCode,
                problems: previousProblems,
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
        document.getElementById("prompt-score").textContent = "?";
        document.getElementById("ct-feedback").textContent = errorMsg || "평가 중 오류가 발생했습니다.";
        document.getElementById("prompt-feedback").textContent = "";
        document.getElementById("ct-score-ring").className = "score-ring";
        document.getElementById("prompt-score-ring").className = "score-ring";
    } else {
        const ctScore = parseInt(data.ct_score) || 0;
        const promptScore = parseInt(data.prompt_score) || 0;
        document.getElementById("ct-score").textContent = ctScore;
        document.getElementById("prompt-score").textContent = promptScore;
        document.getElementById("ct-feedback").textContent = data.ct_feedback || "";
        document.getElementById("prompt-feedback").textContent = data.prompt_feedback || "";
        document.getElementById("ct-score-ring").className = "score-ring " + scoreClass(ctScore);
        document.getElementById("prompt-score-ring").className = "score-ring " + scoreClass(promptScore);
    }

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

async function sendMessage() {
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    input.disabled = true;
    appendBubble("user", message);
    chatHistory.push({ role: "user", content: message });

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
                current_problem: currentProblem,
            }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            const lines = decoder.decode(value).split("\n");
            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const payload = JSON.parse(line.slice(6));
                if (payload.error) {
                    bubble.className = "chat-bubble error";
                    bubble.textContent = payload.error;
                    return;
                }
                if (payload.delta) {
                    fullReply += payload.delta;
                    bubble.textContent = fullReply;
                    document.getElementById("chat-messages").scrollTop = 9999;
                }
            }
        }

        chatHistory.push({ role: "assistant", content: fullReply });
    } finally {
        input.disabled = false;
        input.focus();
    }
}

function appendBubble(role, text) {
    const container = document.getElementById("chat-messages");
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    bubble.textContent = text;
    container.appendChild(bubble);
    container.scrollTop = container.scrollHeight;
    return bubble;
}

function escapeHtml(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

checkStatus();
generateCode();
