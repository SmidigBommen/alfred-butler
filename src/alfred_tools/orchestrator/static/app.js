const messages = document.querySelector("#messages");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const backend = document.querySelector("#backend");
const statusLine = document.querySelector("#status");
const talk = document.querySelector("#talk");
const newSession = document.querySelector("#new-session");
const transcriptionProvider = document.querySelector("#transcription-provider");
const speechOutput = document.querySelector("#speech-output");

let sessionId = crypto.randomUUID();
let recorder = null;
let chunks = [];
let recordingRequested = false;
let microphonePending = false;
let recordingStartedAt = 0;
const minimumRecordingMilliseconds = 300;

function appendInline(parent, tokens) {
  for (const token of tokens) {
    if (token.type === "text") {
      parent.append(document.createTextNode(token.text));
      continue;
    }
    const tag = {
      strong: "strong",
      emphasis: "em",
      code: "code",
      link: "a",
    }[token.type];
    if (!tag) continue;
    const element = document.createElement(tag);
    if (Array.isArray(token.content)) appendInline(element, token.content);
    else element.textContent = token.text;
    if (token.type === "link") {
      element.href = token.url;
      element.target = "_blank";
      element.rel = "noopener noreferrer";
    }
    parent.append(element);
  }
}

function renderMarkdown(article, blocks) {
  for (const block of blocks) {
    if (block.type === "paragraph" || block.type === "heading" || block.type === "quote") {
      const tag =
        block.type === "heading" ? `h${Math.min(Math.max(block.level, 2), 6)}`
        : block.type === "quote" ? "blockquote"
        : "p";
      const element = document.createElement(tag);
      appendInline(element, block.content);
      article.append(element);
      continue;
    }
    if (block.type === "list") {
      const list = document.createElement(block.ordered ? "ol" : "ul");
      for (const item of block.items) {
        const listItem = document.createElement("li");
        appendInline(listItem, item);
        list.append(listItem);
      }
      article.append(list);
      continue;
    }
    if (block.type === "table") {
      const scroll = document.createElement("div");
      scroll.className = "markdown-table-scroll";
      const table = document.createElement("table");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      for (const [column, content] of block.headers.entries()) {
        const cell = document.createElement("th");
        cell.className = `align-${block.align[column]}`;
        appendInline(cell, content);
        headRow.append(cell);
      }
      head.append(headRow);
      table.append(head);
      const body = document.createElement("tbody");
      for (const row of block.rows) {
        const tableRow = document.createElement("tr");
        for (const [column, content] of row.entries()) {
          const cell = document.createElement("td");
          cell.className = `align-${block.align[column]}`;
          appendInline(cell, content);
          tableRow.append(cell);
        }
        body.append(tableRow);
      }
      table.append(body);
      scroll.append(table);
      article.append(scroll);
      continue;
    }
    if (block.type === "code") {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      code.textContent = block.text;
      if (block.language) code.dataset.language = block.language;
      pre.append(code);
      article.append(pre);
      continue;
    }
    if (block.type === "rule") article.append(document.createElement("hr"));
  }
}

function safeWebUrl(value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

function activityList(parent, values, renderItem) {
  if (!Array.isArray(values) || values.length === 0) return;
  const list = document.createElement("ul");
  for (const value of values) {
    const item = document.createElement("li");
    renderItem(item, value);
    list.append(item);
  }
  parent.append(list);
}

function renderActivity(article, tools) {
  if (!Array.isArray(tools) || tools.length === 0) return;
  const details = document.createElement("details");
  details.className = "activity";
  details.open = tools.some((tool) => tool.status !== "completed");
  const toggle = document.createElement("summary");
  const sourceCount = tools.reduce(
    (total, tool) => total + (Number(tool.summary?.source_count) || 0),
    0,
  );
  toggle.textContent = `Activity & sources · ${tools.length} tool${tools.length === 1 ? "" : "s"}${
    sourceCount ? ` · ${sourceCount} fetched` : ""
  }`;
  details.append(toggle);

  const labels = {
    web_search: "Web search",
    web_fetch: "Page fetch",
    web_research: "Web research",
    memory_search: "Memory search",
    memory_capture: "Memory capture",
  };
  for (const tool of tools) {
    const event = document.createElement("section");
    event.className = `tool-event status-${tool.status}`;
    const heading = document.createElement("div");
    heading.className = "tool-event-heading";
    const name = document.createElement("strong");
    name.textContent = labels[tool.name] || tool.name;
    const status = document.createElement("span");
    status.textContent = tool.status;
    status.className = "tool-status";
    heading.append(name, status);
    if (Number.isFinite(tool.duration_ms)) {
      const duration = document.createElement("span");
      duration.className = "tool-duration";
      duration.textContent = `${tool.duration_ms} ms`;
      heading.append(duration);
    }
    event.append(heading);

    const summary = tool.summary || {};
    const queries = summary.queries || (summary.query ? [summary.query] : []);
    if (queries.length) {
      const label = document.createElement("p");
      label.className = "activity-label";
      label.textContent = queries.length === 1 ? "Query" : "Queries";
      event.append(label);
      activityList(event, queries, (item, query) => {
        item.textContent = query;
      });
    }

    const sources = Array.isArray(summary.sources) ? summary.sources : [];
    if (sources.length) {
      const label = document.createElement("p");
      label.className = "activity-label";
      label.textContent = tool.name === "web_search" ? "Search candidates" : "Fetched sources";
      event.append(label);
      activityList(event, sources, (item, source) => {
        const url = safeWebUrl(source.url);
        if (url) {
          const link = document.createElement("a");
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = source.title || url;
          item.append(link);
        } else {
          item.textContent = source.title || "Invalid source URL";
        }
        if (source.cached) item.append(document.createTextNode(" · cached"));
      });
    } else if (Number.isFinite(summary.result_count)) {
      const count = document.createElement("p");
      count.textContent = `${summary.result_count} search results`;
      event.append(count);
    }

    const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
    if (warnings.length) {
      const label = document.createElement("p");
      label.className = "activity-label warning-label";
      label.textContent = "Warnings";
      event.append(label);
      activityList(event, warnings, (item, warning) => {
        item.textContent = [warning.stage, warning.engine, warning.url, warning.error]
          .filter(Boolean)
          .join(" · ");
      });
    }
    if (summary.error) {
      const error = document.createElement("p");
      error.className = "activity-error";
      error.textContent = summary.error;
      event.append(error);
    }
    details.append(event);
  }
  article.append(details);
}

function addMessage(text, kind, blocks = null) {
  const article = document.createElement("article");
  article.className = kind;
  if (kind === "alfred" && Array.isArray(blocks)) renderMarkdown(article, blocks);
  else article.textContent = text;
  messages.append(article);
  article.scrollIntoView({block: "end"});
  return article;
}

function setBusy(busy, text = "") {
  input.disabled = busy;
  document.querySelector("#send").disabled = busy;
  newSession.disabled = busy;
  statusLine.textContent = text || "Text stays in memory and is discarded with this session.";
}

async function loadConfig() {
  const response = await fetch("/api/config", {cache: "no-store"});
  const config = await response.json();
  for (const name of config.backends) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name === "local" ? "LM Studio · local" : "OpenAI · remote";
    backend.append(option);
  }
  backend.value = config.default_backend;
  const inputLabels = {local: "This computer · local", openai: "OpenAI · remote"};
  for (const name of config.voice_input_providers) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = inputLabels[name] || name;
    transcriptionProvider.append(option);
  }
  transcriptionProvider.value = config.voice_input_providers.includes("local")
    ? "local"
    : config.voice_input_providers[0] || "";
  const outputLabels = {browser: "This browser · local", openai: "OpenAI · cedar · British"};
  for (const name of config.voice_output_providers) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = outputLabels[name] || name;
    speechOutput.append(option);
  }
  talk.disabled = config.voice_input_providers.length === 0;
}

async function speakReply(text) {
  if (speechOutput.value === "off") return;
  if (speechOutput.value === "browser") {
    if ("speechSynthesis" in window) {
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en";
      speechSynthesis.speak(utterance);
    }
    return;
  }
  const response = await fetch("/api/speech", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({provider: speechOutput.value, text}),
  });
  if (!response.ok) {
    const result = await response.json();
    throw new Error(result.error || "Speech generation failed");
  }
  const audioUrl = URL.createObjectURL(await response.blob());
  const player = new Audio(audioUrl);
  player.addEventListener("ended", () => URL.revokeObjectURL(audioUrl), {once: true});
  await player.play();
}

async function sendMessage(text) {
  addMessage(text, "user");
  setBusy(
    true,
    backend.value === "remote"
      ? "Sending to OpenAI…"
      : "Local model is working; first response can take several minutes…",
  );
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId, backend: backend.value, message: text}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed");
    const answer = addMessage(result.answer, "alfred", result.answer_blocks);
    renderActivity(answer, result.tools);
    await speakReply(result.answer);
  } catch (error) {
    addMessage(error.message, "error");
  } finally {
    setBusy(false);
    input.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text);
});

function resetSession() {
  sessionId = crypto.randomUUID();
  messages.replaceChildren();
  addMessage(`New ${backend.value} session. Previous context was discarded.`, "alfred");
  input.focus();
}

backend.addEventListener("change", resetSession);
newSession.addEventListener("click", resetSession);

async function startRecording() {
  recordingRequested = true;
  if (recorder?.state === "recording" || microphonePending || talk.disabled) return;
  microphonePending = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (!recordingRequested) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    const currentRecorder = recorder;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    });
    recorder.addEventListener("stop", async () => {
      stream.getTracks().forEach((track) => track.stop());
      if (recorder === currentRecorder) recorder = null;
      talk.classList.remove("recording");
      talk.textContent = "Hold to talk";
      try {
        const duration = performance.now() - recordingStartedAt;
        const audio = new Blob(chunks, {type: currentRecorder.mimeType || "audio/webm"});
        if (duration < minimumRecordingMilliseconds || audio.size < 512) {
          throw new Error("Hold push-to-talk a little longer before releasing it.");
        }
        const remoteAudio = transcriptionProvider.value === "openai";
        setBusy(true, remoteAudio ? "Sending audio to OpenAI…" : "Transcribing locally…");
        const provider = encodeURIComponent(transcriptionProvider.value);
        const response = await fetch(`/api/transcribe?provider=${provider}`, {
          method: "POST",
          headers: {"Content-Type": audio.type},
          body: audio,
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Transcription failed");
        if (result.text.trim()) await sendMessage(result.text.trim());
      } catch (error) {
        addMessage(error.message, "error");
      } finally {
        setBusy(false);
      }
    });
    recorder.start();
    recordingStartedAt = performance.now();
    talk.classList.add("recording");
    talk.textContent = "Listening…";
    statusLine.textContent = "Release Shift+CapsLock to send";
  } catch (error) {
    addMessage(`Microphone unavailable: ${error.message}`, "error");
  } finally {
    microphonePending = false;
  }
}

function stopRecording() {
  recordingRequested = false;
  if (recorder?.state === "recording") recorder.stop();
}

window.addEventListener("keydown", (event) => {
  if (event.code === "CapsLock" && event.shiftKey && !event.repeat) {
    event.preventDefault();
    startRecording();
  }
});
window.addEventListener("keyup", (event) => {
  if (event.code === "CapsLock") {
    event.preventDefault();
    stopRecording();
  }
});
talk.addEventListener("pointerdown", startRecording);
talk.addEventListener("pointerup", stopRecording);
talk.addEventListener("pointercancel", stopRecording);

loadConfig().catch((error) => addMessage(error.message, "error"));
