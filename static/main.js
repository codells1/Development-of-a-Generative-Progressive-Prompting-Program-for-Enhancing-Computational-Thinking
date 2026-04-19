const chatHistory = [];
const sessionId = `${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
let currentCodeContext = null;

async function generateCode() {
    const topic = document.getElementById("topic-input").value.trim() || "Python 기초";
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

    // 코드 생성 직후 자동으로 문제 생성
    generateProblem(data.code);
}

async function generateProblem(code) {
    const problemDisplay = document.getElementById("problem-display");

    const res = await fetch("/api/generate-problem", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code }),
    });

    const data = await res.json();
    problemDisplay.className = "text-display";

    if (data.error) {
        problemDisplay.style.color = "#e53e3e";
        problemDisplay.textContent = data.error;
        return;
    }

    problemDisplay.style.color = "";
    problemDisplay.textContent = data.problem;
}

async function sendMessage() {
    const input = document.getElementById("chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    appendBubble("user", message);
    chatHistory.push({ role: "user", content: message });

    const loadingBubble = appendBubble("assistant", "답변 생성 중", true);

    const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: chatHistory, session_id: sessionId, code_context: currentCodeContext }),
    });

    const data = await res.json();
    loadingBubble.remove();

    if (data.error) {
        appendBubble("error", data.error);
        return;
    }

    chatHistory.push({ role: "assistant", content: data.reply });
    appendBubble("assistant", data.reply);
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
