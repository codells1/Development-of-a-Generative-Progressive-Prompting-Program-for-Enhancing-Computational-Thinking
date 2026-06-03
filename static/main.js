const chatHistory = [];
const sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
let currentCode = null;
let currentProblemData = null;   // 현재 MCQ 객체 {question, options, answer, explanation, ct_skill}
let currentProblemIndex = 0;
let previousProblems = [];       // 문제 본문 문자열 배열 (중복 방지 + 평가용)
let submittedAnswers = [];       // 학생 선택 라벨 배열 (A/B/C/D)
let selectedOption = null;       // 현재 선택된 라벨
let currentTopic = "";
const TOTAL_PROBLEMS = 5;

const STAGES = ["변수·연산·조건", "반복·리스트", "함수", "알고리즘"];
let currentStage = 0;
let isFusion = false;

// 수준 2: 직전 세션·단계의 CT 약점 (챗봇 프롬프트 주입용)
let prevWeakCt = null;

// ── 대화 최소 조건 설정값 (여기서만 수정) ───────────────────────
// 세션 내 의미 있는 대화로 인정받는 최소 횟수 (권장 5~8)
const MIN_CHAT_TURNS             = 6;
// 마지막 입력 후 챗봇이 먼저 말을 거는 무입력 대기 시간, 초 (권장 30~90)
const INACTIVITY_TIMEOUT_SEC     = 45;
// 의미 있는 메시지로 인정하는 최소 자수 — 단답 제외 (권장 8~15)
const MIN_MSG_LEN                = 10;
// 오답 판정 후 챗봇 유도 메시지를 보내기까지의 딜레이, ms (권장 1000~3000)
const WRONG_NUDGE_DELAY_MS       = 1500;
// 문제 전체 완료 후 첫 유도 메시지를 보내기까지의 딜레이, ms
const POST_COMPLETE_NUDGE_DELAY_MS = 600;
// ────────────────────────────────────────────────────────────────

let qualityChatCount    = 0;      // 품질 충족 대화 수 (세션 내)
let inactivityTimer     = null;   // 무입력 타이머
let wrongNudgeTimer     = null;   // 오답 유도 타이머
let nudgeInProgress     = false;  // 유도 중복 방지
let allProblemsComplete = false;  // 문제 완료 후 최소 대화 대기 중
let stageChatHistory    = [];     // 현 단계 전용 대화 기록 (CT 측정용, 단계 전환마다 초기화)

const CT_TIP = {
    "분해":          "코드를 역할별로 나눠서 각 부분이 어떻게 동작하는지 질문해보세요.",
    "패턴인식":      "코드에서 반복되는 패턴이나 규칙을 발견하는 질문을 해보세요.",
    "추상화":        "변수나 함수가 실제로 무엇을 대표하는지 질문해보세요.",
    "알고리즘적사고": "코드 실행 순서를 단계별로 추적하며 결과를 예측하는 질문을 해보세요.",
};

async function init() {
    checkStatus();
    updateChatCount();   // 배지를 상수값으로 즉시 초기화
    await loadPreviousFeedback();
}

async function loadPreviousFeedback() {
    try {
        const res = await fetch("/api/previous-feedback");
        const data = await res.json();
        if (!data.has_previous) { generateCode(); return; }
        prevWeakCt = data.weak_ct || null;
        showSessionStartOverlay(data);
    } catch {
        generateCode();
    }
}

function showSessionStartOverlay(data) {
    document.getElementById("prev-topic").textContent = data.topic || "";

    const container = document.getElementById("prev-ct-scores");
    container.innerHTML = "";
    const ctScores = data.ct_scores || {};
    const weakCt = data.weak_ct || "";

    for (const [skill, score] of Object.entries(ctScores)) {
        const isWeak = skill === weakCt;
        const row = document.createElement("div");
        row.className = "ct-bar-row";
        row.innerHTML =
            `<span class="ct-bar-label${isWeak ? " weak" : ""}">${escapeHtml(skill)}</span>` +
            `<div class="ct-bar-track">` +
            `<div class="ct-bar-fill${isWeak ? " weak" : ""}" style="width:${score / 5 * 100}%"></div>` +
            `</div>` +
            `<span class="ct-bar-score">${score}/5</span>`;
        container.appendChild(row);
    }

    const tipEl = document.getElementById("weak-ct-tip");
    if (weakCt && CT_TIP[weakCt]) {
        tipEl.innerHTML =
            `지난번 <strong>'${escapeHtml(weakCt)}'</strong>이(가) 약했어요.<br>${escapeHtml(CT_TIP[weakCt])}`;
        tipEl.classList.remove("hidden");
    } else {
        tipEl.classList.add("hidden");
    }

    document.getElementById("session-start-overlay").classList.remove("hidden");
}

function startLearning() {
    document.getElementById("session-start-overlay").classList.add("hidden");
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
    const stageLabel = isFusion
        ? "융합 종합 평가"
        : `${currentStage + 1}단계: ${STAGES[currentStage]}`;
    currentTopic = isFusion ? "융합" : STAGES[currentStage];

    const codeBlock = document.getElementById("code-display");
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");

    currentProblemIndex = 0;
    previousProblems = [];
    submittedAnswers = [];
    currentProblemData = null;
    selectedOption = null;
    qualityChatCount = 0;
    allProblemsComplete = false;
    stageChatHistory = [];
    clearInactivityTimer();
    updateChatCount();

    document.getElementById("topic-label").textContent = stageLabel;
    setOverlay(true, `${stageLabel} 코드 만드는 중...`);
    codeBlock.innerHTML = "<code></code>";
    problemDisplay.className = "text-display placeholder";
    problemDisplay.textContent = "";
    document.getElementById("problem-count").textContent = "";
    answerArea.classList.remove("visible");

    try {
        const body = isFusion ? { is_fusion: true } : { stage: currentStage };
        const res = await fetch("/api/generate-code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();

        if (data.error) {
            codeBlock.innerHTML = `<code style="color:#e53e3e">${data.error}</code>`;
            return;
        }

        codeBlock.innerHTML = `<code>${escapeHtml(data.code)}</code>`;
        currentCode = data.code;
        currentTopic = data.topic || currentTopic;

        setOverlay(true, `문제 1 / ${TOTAL_PROBLEMS} 만드는 중...`);
        await generateNextProblem();
        resetInactivityTimer();
    } catch (e) {
        codeBlock.innerHTML = `<code style="color:#e53e3e">오류: ${e.message}</code>`;
    } finally {
        setOverlay(false);
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

async function generateNextProblem() {
    const problemDisplay = document.getElementById("problem-display");
    const answerArea = document.getElementById("answer-area");

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

        currentProblemData = data;
        const question = (data.question || "").trim();
        if (!question) {
            problemDisplay.className = "text-display";
            problemDisplay.style.color = "#e53e3e";
            problemDisplay.textContent = "문제를 생성하지 못했습니다.";
            return;
        }

        previousProblems.push(question);
        selectedOption = null;

        const ctSkill = data.ct_skill || "";
        document.getElementById("problem-count").textContent =
            `${currentProblemIndex + 1} / ${TOTAL_PROBLEMS}`;
        problemDisplay.className = "text-display";
        problemDisplay.style.color = "";
        problemDisplay.innerHTML =
            `${ctSkill ? `<div class="ct-skill-badge">${escapeHtml(ctSkill)}</div>` : ""}` +
            `<div class="problem-item">${escapeHtml(question)}</div>`;

        renderOptions(data.options);
        document.getElementById("submit-btn").disabled = true;

        const feedbackArea = document.getElementById("feedback-area");
        feedbackArea.classList.add("hidden");
        feedbackArea.classList.remove("wrong-bg");
        document.getElementById("ack-btn").classList.add("hidden");
        document.getElementById("next-btn").classList.add("hidden");

        document.getElementById("answer-submit-row").classList.remove("hidden");
        answerArea.classList.add("visible");
    } catch (e) {
        console.error("[문제생성] 오류:", e);
        problemDisplay.className = "text-display";
        problemDisplay.style.color = "#e53e3e";
        problemDisplay.textContent = `문제 생성 오류: ${e.message}`;
    }
}

// ── Task 10: 대화 최소 조건 헬퍼 ────────────────────────────────

function isQualityMessage(msg) {
    return msg.trim().length >= MIN_MSG_LEN;
}

function updateChatCount() {
    const el = document.getElementById("chat-count");
    if (!el) return;
    const met = qualityChatCount >= MIN_CHAT_TURNS;
    el.textContent = `${qualityChatCount} / ${MIN_CHAT_TURNS}회`;
    el.className = "chat-count-badge" + (met ? " met" : "");
    const remainingEl = document.getElementById("remaining-count");
    if (remainingEl) {
        remainingEl.textContent = Math.max(0, MIN_CHAT_TURNS - qualityChatCount);
    }
}

function clearInactivityTimer() {
    if (inactivityTimer) { clearTimeout(inactivityTimer); inactivityTimer = null; }
}

function resetInactivityTimer() {
    clearInactivityTimer();
    inactivityTimer = setTimeout(() => {
        triggerNudge("inactivity");
    }, INACTIVITY_TIMEOUT_SEC * 1000);
}

async function triggerNudge(reason) {
    if (nudgeInProgress) return;
    nudgeInProgress = true;
    clearInactivityTimer();   // 중복 유도 방지 — 남아있는 타이머 즉시 해제

    const bubble = appendBubble("assistant", "");
    let fullReply = "";

    try {
        const res = await fetch("/api/chat-nudge", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                messages: chatHistory,
                session_id: sessionId,
                code_context: currentCode,
                current_problem: currentProblemData ? currentProblemData.question : null,
                weak_ct: prevWeakCt,
                reason,
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
                    bubble.textContent = fullReply.trimStart();
                    document.getElementById("chat-messages").scrollTop = 9999;
                }
            }
        }

        if (fullReply) {
            chatHistory.push({ role: "assistant", content: fullReply });
            stageChatHistory.push({ role: "assistant", content: fullReply });
        }
    } catch (e) {
        bubble.className = "chat-bubble error";
        bubble.textContent = "유도 메시지 오류";
    } finally {
        nudgeInProgress = false;
        // 타이머 재시작 안 함 — 사용자가 메시지를 보낼 때(sendMessage)만 재시작
    }
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

    // 피드백 배지
    const badge = document.getElementById("feedback-badge");
    badge.className = "feedback-badge " + (isCorrect ? "feedback-correct" : "feedback-wrong");
    badge.textContent = isCorrect ? "✓ 정답" : "✗ 오답";
    document.getElementById("feedback-explanation").textContent =
        isCorrect ? (currentProblemData.explanation || "") : "";

    // 오답: 연한 빨강 배경 + "이해했어요" 버튼만 표시 (다음 문제 잠금)
    // 정답: 바로 "다음 문제 →" 표시
    const feedbackArea = document.getElementById("feedback-area");
    const ackBtn  = document.getElementById("ack-btn");
    const nextBtn = document.getElementById("next-btn");

    if (isCorrect) {
        feedbackArea.classList.remove("wrong-bg");
        ackBtn.classList.add("hidden");
        nextBtn.classList.remove("hidden");
    } else {
        feedbackArea.classList.add("wrong-bg");
        ackBtn.classList.remove("hidden");
        nextBtn.classList.add("hidden");
        wrongNudgeTimer = setTimeout(() => triggerNudge("wrong_answer"), WRONG_NUDGE_DELAY_MS);
    }

    document.getElementById("answer-submit-row").classList.add("hidden");
    feedbackArea.classList.remove("hidden");
}

function acknowledgeWrong() {
    if (wrongNudgeTimer) { clearTimeout(wrongNudgeTimer); wrongNudgeTimer = null; }
    document.getElementById("ack-btn").classList.add("hidden");
    document.getElementById("next-btn").classList.remove("hidden");
}

async function nextProblem() {
    currentProblemIndex++;

    if (currentProblemIndex >= TOTAL_PROBLEMS) {
        clearInactivityTimer();
        document.getElementById("problem-count").textContent = "완료";
        const problemDisplay = document.getElementById("problem-display");
        const answerArea = document.getElementById("answer-area");
        answerArea.classList.remove("visible");

        if (qualityChatCount < MIN_CHAT_TURNS) {
            allProblemsComplete = true;
            const remaining = MIN_CHAT_TURNS - qualityChatCount;
            problemDisplay.className = "text-display";
            problemDisplay.innerHTML =
                `<div class="complete-msg">모든 문제를 완료했어요!</div>` +
                `<div class="chat-nudge-msg">평가 전에 챗봇과 조금 더 대화해볼까요?<br>` +
                `<span id="remaining-count">${remaining}</span>회 더 질문하면 평가가 시작됩니다.</div>`;
            setTimeout(() => triggerNudge("inactivity"), POST_COMPLETE_NUDGE_DELAY_MS);
            return;
        }

        problemDisplay.className = "text-display";
        problemDisplay.innerHTML = `<div class="complete-msg">모든 문제를 완료했습니다! 평가 중...</div>`;
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
    clearInactivityTimer();
    setOverlay(true, "학습 결과 평가 중...");
    try {
        const res = await fetch("/api/evaluate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                session_id: sessionId,
                topic: currentTopic,
                code: currentCode,
                problems: previousProblems,
                answers: submittedAnswers,
                chat_history: stageChatHistory,
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
        // 수준 2: 이 단계 약점을 다음 단계 챗봇에 즉시 반영
        if (data.weak_ct) prevWeakCt = data.weak_ct;

        const ctScore = parseInt(data.ct_score) || 0;
        const promptScore = parseInt(data.prompt_score) || 0;
        document.getElementById("ct-score").textContent = ctScore;
        document.getElementById("prompt-score").textContent = promptScore;
        document.getElementById("ct-feedback").textContent = data.ct_feedback || "";
        document.getElementById("prompt-feedback").textContent = data.prompt_feedback || "";
        document.getElementById("ct-score-ring").className = "score-ring " + scoreClass(ctScore);
        document.getElementById("prompt-score-ring").className = "score-ring " + scoreClass(promptScore);
    }

    const nextBtn = document.querySelector(".eval-next-btn");
    if (isFusion) {
        nextBtn.textContent = "처음으로 →";
    } else if (currentStage >= STAGES.length - 1) {
        nextBtn.textContent = "융합 평가 →";
    } else {
        nextBtn.textContent = `${currentStage + 2}단계로 →`;
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
    if (isFusion) {
        currentStage = 0;
        isFusion = false;
    } else if (currentStage >= STAGES.length - 1) {
        isFusion = true;
    } else {
        currentStage++;
    }
    generateCode();
}

async function sendMessage() {
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    const isQuality = isQualityMessage(message);

    input.value = "";
    input.disabled = true;
    appendBubble("user", message);
    chatHistory.push({ role: "user", content: message });
    stageChatHistory.push({ role: "user", content: message });

    if (isQuality) {
        qualityChatCount++;
        updateChatCount();
    }
    resetInactivityTimer();

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
                weak_ct: prevWeakCt,
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
                    bubble.textContent = fullReply.trimStart();
                    document.getElementById("chat-messages").scrollTop = 9999;
                }
            }
        }

        chatHistory.push({ role: "assistant", content: fullReply });
        stageChatHistory.push({ role: "assistant", content: fullReply });

        // 문제 완료 후 최소 대화 달성 → 평가 자동 진입
        if (isQuality && allProblemsComplete && qualityChatCount >= MIN_CHAT_TURNS) {
            allProblemsComplete = false;
            const problemDisplay = document.getElementById("problem-display");
            problemDisplay.innerHTML = `<div class="complete-msg">대화 완료! 평가를 시작합니다...</div>`;
            await triggerEvaluation();
        }
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

function skipToNext() {
    if (isFusion) {
        currentStage = 0;
        isFusion = false;
    } else if (currentStage >= STAGES.length - 1) {
        isFusion = true;
    } else {
        currentStage++;
    }
    generateCode();
}

init();
