/* ==========================================================================
   chat.js
   Wires up #chat-section: conversational Q&A backed by POST /jobs/{job_id}/chat.

   Features:
     - Conversational layout: User questions right-aligned, AI answers left-aligned.
     - Animated "Thinking..." state while waiting for AI response.
     - Sequential Typewriter Effect (word-by-word streaming animation).
     - Full client-side history maintenance.
   ========================================================================== */

(function () {
  const chatMessages = document.getElementById("chat-messages");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const chatSendButton = document.getElementById("chat-send-button");

  // [{role: "user"|"assistant", content: string}, ...]
  let history = [];

  window.resetChat = function () {
    history = [];
    if (chatMessages) chatMessages.innerHTML = "";
    if (chatInput) chatInput.value = "";
  };

  if (!chatForm) return;

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const jobId = window.currentJobId;
    const message = chatInput.value.trim();
    if (!jobId || !message) return;

    // 1. Add user message (right-aligned)
    appendMessage("user", message);
    chatInput.value = "";
    chatInput.disabled = true;
    if (chatSendButton) chatSendButton.disabled = true;

    // 2. Add animated thinking indicator (left-aligned)
    const thinkingRow = appendThinkingMessage();

    try {
      const reply = await sendChatMessage(jobId, message, history);
      
      // 3. Remove thinking animation and run typewriter effect
      thinkingRow.remove();
      const assistantBubble = appendMessage("assistant", "");
      
      await typeWriterText(assistantBubble, reply);

      history.push({ role: "user", content: message });
      history.push({ role: "assistant", content: reply });
    } catch (err) {
      console.error(err);
      thinkingRow.remove();
      const errBubble = appendMessage("assistant", `Error: ${err.message}`);
      errBubble.classList.add("chat-message-error");
    } finally {
      chatInput.disabled = false;
      if (chatSendButton) chatSendButton.disabled = false;
      chatInput.focus();
    }
  });

  async function sendChatMessage(jobId, message, history) {
    const response = await fetch(`/jobs/${jobId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history }),
    });

    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Chat request failed (HTTP ${response.status})`);
    }

    const data = await response.json();
    return data.reply;
  }

  function appendMessage(role, text) {
    const row = document.createElement("div");
    row.className = `chat-message-row ${role}-row`;

    const avatar = document.createElement("div");
    avatar.className = "chat-avatar";
    avatar.textContent = role === "user" ? "👤" : "🤖";

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = text;

    row.appendChild(avatar);
    row.appendChild(bubble);

    chatMessages.appendChild(row);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    return bubble;
  }

  function appendThinkingMessage() {
    const row = document.createElement("div");
    row.className = "chat-message-row assistant-row";

    const avatar = document.createElement("div");
    avatar.className = "chat-avatar";
    avatar.textContent = "🤖";

    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";

    const thinkingBox = document.createElement("div");
    thinkingBox.className = "thinking-box";
    thinkingBox.innerHTML = `
      <span class="dots-span"><span></span><span></span><span></span></span>
      <span>Thinking &amp; analyzing tracking data...</span>
    `;

    bubble.appendChild(thinkingBox);
    row.appendChild(avatar);
    row.appendChild(bubble);

    chatMessages.appendChild(row);
    chatMessages.scrollTop = chatMessages.scrollHeight;

    return row;
  }

  function typeWriterText(bubbleElement, text) {
    return new Promise((resolve) => {
      bubbleElement.textContent = "";
      const words = text.split(" ");
      let idx = 0;

      function typeNext() {
        if (idx < words.length) {
          bubbleElement.textContent += (idx === 0 ? "" : " ") + words[idx];
          idx++;
          chatMessages.scrollTop = chatMessages.scrollHeight;
          setTimeout(typeNext, 30); // 30ms per word
        } else {
          resolve();
        }
      }

      typeNext();
    });
  }
})();
