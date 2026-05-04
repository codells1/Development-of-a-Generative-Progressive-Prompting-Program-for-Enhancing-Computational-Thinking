const chatHistory = [];
const sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
let currentCodeContext = null;

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
    const topic = document.getElementById("topic-input")?.value.trim() || "Python 기초";
    const codeBlock = document.getElementById("code-display");
    const problemDisplay = document.getElementById("problem-display");

    codeBlock.innerHTML = '<code class="loading">생성 중</code>';
    problemDisplay.className = "text-display placeholder loading";
    problemDisplay.textContent = "문제 생성 중";

    const res = await fetch("/api/generate-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic }),
    });

    const data = await res.json();

    if (data.error) {
        codeBlock.innerHTML = `<code style="color:#e53e3e">${data.error}</code>`;
        problemDisplay.className = "text-display";
        problemDisplay.textContent = "";
        return;
    }

    codeBlock.innerHTML = `<code>${escapeHtml(data.code)}</code>`;
    currentCodeContext = topic;
    generateProblems(data.code);
}

// ── 문제 생성 (5개 고정 순서로 전체 표시) ────────────────
async function generateProblems(code) {
    const res = await fetch("/api/generate-problem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
    });

    const data = await res.json();
    const problemDisplay = document.getElementById("problem-display");
    problemDisplay.className = "text-display";

    if (data.error) {
        problemDisplay.style.color = "#e53e3e";
        problemDisplay.textContent = data.error;
        return;
    }

    problemDisplay.style.color = "";
    const total = data.problems.length;
    document.getElementById("problem-count").textContent = `[1/${total}]`;
    problemDisplay.innerHTML = data.problems
        .map((p, i) => `<div class="problem-item"><span class="problem-num">[${i + 1}/${total}]</span>${escapeHtml(p)}</div>`)
        .join("");
}

// ── 챗봇 (스트리밍) ──────────────────────────────────────
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
            body: JSON.stringify({ messages: chatHistory, session_id: sessionId, code_context: currentCodeContext }),
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

function appendBubble(role, text, isLoading = false) {
    const container = document.getElementById("chat-messages");
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}${isLoading ? " loading" : ""}`;
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

// 초기 실행
checkStatus();
