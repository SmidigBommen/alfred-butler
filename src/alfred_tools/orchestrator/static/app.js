const messages = document.querySelector("#messages");
const form = document.querySelector("#chat-form");
const input = document.querySelector("#message");
const backend = document.querySelector("#backend");
const statusLine = document.querySelector("#status");
const talk = document.querySelector("#talk");
const transcriptionProvider = document.querySelector("#transcription-provider");
const speechOutput = document.querySelector("#speech-output");

let sessionId = crypto.randomUUID();
let recorder = null;
let chunks = [];
let recordingRequested = false;
let microphonePending = false;
let recordingStartedAt = 0;
const minimumRecordingMilliseconds = 300;

function addMessage(text, kind) {
  const article = document.createElement("article");
  article.className = kind;
  article.textContent = text;
  messages.append(article);
  article.scrollIntoView({block: "end"});
}

function setBusy(busy, text = "") {
  input.disabled = busy;
  document.querySelector("#send").disabled = busy;
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
  const outputLabels = {browser: "This browser · local", openai: "OpenAI · marin"};
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
    body: JSON.stringify({provider: speechOutput.value, voice: "marin", text}),
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
  setBusy(true, backend.value === "remote" ? "Sending to OpenAI…" : "Thinking locally…");
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId, backend: backend.value, message: text}),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed");
    addMessage(result.answer, "alfred");
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

backend.addEventListener("change", () => {
  sessionId = crypto.randomUUID();
  messages.replaceChildren();
  addMessage(`New ${backend.value} session. Previous context was discarded.`, "alfred");
});

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
