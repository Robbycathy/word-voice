"use strict";

const form = document.querySelector("#speech-form");
const textInput = document.querySelector("#text-input");
const submitButton = document.querySelector("#submit-button");
const engineOptions = document.querySelectorAll('input[name="engine"]');
const voiceSelect = document.querySelector("#voice-select");
const voiceStatus = document.querySelector("#voice-status");
const reloadVoices = document.querySelector("#reload-voices");
const statusMessage = document.querySelector("#status-message");
const audio = document.querySelector("#audio-player");
const voiceCache = new Map();
const selectedVoices = new Map();
let busy = false;
let voicesLoading = false;
let voicesReady = false;
let currentVoiceEngine = "online";
let voiceSequence = 0;
let speechSequence = 0;
let voiceController = null;
let speechController = null;
let audioUrl = null;

function selectedEngine() {
  return document.querySelector('input[name="engine"]:checked').value;
}

function showStatus(message, isError = false) {
  statusMessage.textContent = message;
  statusMessage.classList.toggle("error", isError);
  statusMessage.hidden = !message;
}

function showVoiceStatus(message, isError = false) {
  voiceStatus.textContent = message;
  voiceStatus.classList.toggle("error", isError);
  voiceStatus.hidden = !message;
}

function updateControls() {
  textInput.disabled = busy;
  engineOptions.forEach((option) => { option.disabled = busy; });
  voiceSelect.disabled = busy || voicesLoading || !voicesReady;
  submitButton.disabled = busy || voicesLoading || !voicesReady || !voiceSelect.value;
  reloadVoices.disabled = busy || voicesLoading;
  submitButton.textContent = busy ? "正在生成…" : "生成并播放";
  form.setAttribute("aria-busy", String(busy || voicesLoading));
  voiceSelect.setAttribute("aria-busy", String(voicesLoading));
}

function clearAudio() {
  audio.pause();
  audio.hidden = true;
  audio.removeAttribute("src");
  audio.load();
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = null;
}

function emptyVoiceSelect(message) {
  voiceSelect.replaceChildren(new Option(message, ""));
  voicesReady = false;
}

function applyVoiceList(engine, result) {
  voiceSelect.replaceChildren();
  for (const voice of result.voices) {
    voiceSelect.add(new Option(voice.label, voice.id));
  }
  if (!result.voices.length) {
    emptyVoiceSelect("暂无可用声音");
    showVoiceStatus("当前语音类型暂无可用声音，可以重新加载或切换语音类型。", true);
    reloadVoices.hidden = false;
    return;
  }
  const saved = selectedVoices.get(engine);
  const preferred = result.voices.some((voice) => voice.id === saved) ? saved : result.default_voice;
  voiceSelect.value = result.voices.some((voice) => voice.id === preferred) ? preferred : result.voices[0].id;
  selectedVoices.set(engine, voiceSelect.value);
  voicesReady = true;
  showVoiceStatus("");
  reloadVoices.hidden = true;
}

async function responseError(response, fallback) {
  try {
    const result = await response.json();
    return typeof result.detail === "string" ? result.detail : fallback;
  } catch {
    return fallback;
  }
}

async function loadVoices(forceReload = false) {
  const engine = selectedEngine();
  const sequence = ++voiceSequence;
  if (voiceController) voiceController.abort();
  voiceController = null;
  currentVoiceEngine = engine;
  clearAudio();
  showStatus("");
  voicesLoading = true;
  emptyVoiceSelect("正在加载声音…");
  showVoiceStatus("正在加载声音…");
  reloadVoices.hidden = true;
  updateControls();

  if (!forceReload && voiceCache.has(engine)) {
    applyVoiceList(engine, voiceCache.get(engine));
    voicesLoading = false;
    updateControls();
    return;
  }

  const controller = new AbortController();
  voiceController = controller;
  const timeout = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(`/api/voices?engine=${encodeURIComponent(engine)}`, {
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error(await responseError(response, "声音列表加载失败，请重试。"));
    const result = await response.json();
    if (sequence !== voiceSequence) return;
    if (result.engine !== engine || !Array.isArray(result.voices)) throw new Error("声音列表格式有误，请重新加载。");
    const validVoices = result.voices.every((voice) => voice && typeof voice.id === "string" && voice.id && typeof voice.label === "string" && voice.label);
    if (!validVoices) throw new Error("声音列表格式有误，请重新加载。");
    if (result.voices.length) voiceCache.set(engine, result);
    applyVoiceList(engine, result);
  } catch (error) {
    if (sequence !== voiceSequence) return;
    emptyVoiceSelect("声音列表未加载");
    const message = error.name === "AbortError"
      ? "声音列表加载超时，请重新加载或切换语音类型。"
      : error instanceof TypeError
        ? "无法加载声音，请确认 Python 服务已启动后重试。"
        : error.message;
    showVoiceStatus(message, true);
    reloadVoices.hidden = false;
  } finally {
    clearTimeout(timeout);
    if (sequence === voiceSequence) {
      voiceController = null;
      voicesLoading = false;
      updateControls();
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || voicesLoading || !voicesReady || !voiceSelect.value) return;
  const text = textInput.value.trim();
  if (!text || text.length > 1000 || !/[A-Za-z]/.test(text)) {
    textInput.setAttribute("aria-invalid", "true");
    showStatus(!text ? "请输入英文单词或句子。" : text.length > 1000 ? "输入内容不能超过 1000 个字符。" : "输入内容需要包含英文字母。", true);
    textInput.focus();
    return;
  }

  textInput.removeAttribute("aria-invalid");
  const engine = selectedEngine();
  const voice = voiceSelect.value;
  const sequence = ++speechSequence;
  clearAudio();
  busy = true;
  updateControls();
  showStatus("正在生成音频，请稍候…");
  const controller = new AbortController();
  speechController = controller;
  const timeout = setTimeout(() => controller.abort(), 75000);

  try {
    const response = await fetch("/api/pronounce", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, engine, voice }),
      signal: controller.signal,
    });
    if (!response.ok) {
      if (response.status === 422 && sequence === speechSequence) {
        voiceCache.delete(engine);
        reloadVoices.hidden = false;
      }
      throw new Error(await responseError(response, "生成失败，请稍后重试。"));
    }
    const blob = await response.blob();
    if (sequence !== speechSequence) return;
    if (!blob.size || !blob.type.startsWith("audio/")) throw new Error("没有收到有效音频，请重试。");
    audioUrl = URL.createObjectURL(blob);
    audio.src = audioUrl;
    audio.hidden = false;
    showStatus("音频已生成。");
    try {
      await audio.play();
    } catch (error) {
      if (sequence !== speechSequence) return;
      if (error.name === "NotAllowedError") {
        showStatus("音频已生成，请点击播放器的播放按钮。");
      } else if (error.name !== "AbortError") {
        showStatus("音频暂时无法播放，请重新生成。", true);
      }
    }
  } catch (error) {
    if (sequence !== speechSequence) return;
    const message = error.name === "AbortError"
      ? "生成超时，请检查网络后重试。"
      : error instanceof TypeError
        ? "无法连接后端，请确认 Python 服务已启动。"
        : error.message;
    showStatus(message, true);
  } finally {
    clearTimeout(timeout);
    if (sequence === speechSequence) {
      speechController = null;
      busy = false;
      updateControls();
    }
  }
});

textInput.addEventListener("input", () => {
  textInput.removeAttribute("aria-invalid");
  showStatus("");
});

engineOptions.forEach((option) => {
  option.addEventListener("change", () => { loadVoices(); });
});

voiceSelect.addEventListener("change", () => {
  selectedVoices.set(currentVoiceEngine, voiceSelect.value);
  clearAudio();
  showStatus("");
  updateControls();
});

reloadVoices.addEventListener("click", () => { loadVoices(true); });

audio.addEventListener("error", () => {
  if (audioUrl) showStatus("音频暂时无法播放，请重新生成。", true);
});

textInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    form.requestSubmit();
  }
});

window.addEventListener("pagehide", () => {
  ++voiceSequence;
  ++speechSequence;
  if (voiceController) voiceController.abort();
  if (speechController) speechController.abort();
  busy = false;
  voicesLoading = false;
  clearAudio();
});

window.addEventListener("pageshow", (event) => {
  if (event.persisted) loadVoices();
});

loadVoices();
