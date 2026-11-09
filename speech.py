"""Generate English pronunciation audio, with a bounded in-memory cache."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import logging
import io
from pathlib import Path
import platform
import shutil
import tempfile
import time
import wave
from typing import Dict, Optional, Tuple

import edge_tts

logger = logging.getLogger(__name__)
VOICES = {"us": "en-US-AriaNeural", "uk": "en-GB-SoniaNeural"}
LOCAL_VOICES = {"us": "Samantha", "uk": "Daniel"}
RATES = {"normal": "+0%", "slow": "-25%"}


class SpeechUnavailable(Exception):
    """A user-facing speech service failure."""


@dataclass(frozen=True)
class Audio:
    data: bytes
    media_type: str
    extension: str
    engine: str
    voice: str


class SpeechService:
    def __init__(self, engine: str = "auto", timeout: float = 30.0):
        if engine not in {"auto", "edge", "macos"}:
            raise ValueError("TTS_ENGINE 必须是 auto、edge 或 macos")
        if timeout <= 0:
            raise ValueError("TTS_TIMEOUT 必须大于 0")
        self.engine = engine
        self.timeout = timeout
        self.cache = OrderedDict()
        self.cache_limit = 128
        self.cache_byte_limit = 32 * 1024 * 1024
        self.cache_bytes = 0
        self._online_retry_at = 0.0
        self._inflight: Dict[Tuple[str, str, str, Optional[str]], asyncio.Task] = {}
        self._slots = asyncio.Semaphore(4)

    @property
    def local_available(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("say") is not None

    async def pronounce(self, text: str, accent: str, speed: str, voice: Optional[str] = None):
        # Preserve case: US and us, for example, can have different pronunciations.
        key = (text, accent, speed, voice)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1], True
        expired = self.cache.pop(key, None)
        if expired:
            self.cache_bytes -= len(expired[1].data)
        if key not in self._inflight:
            task = asyncio.create_task(self._generate_and_cache(key))
            self._inflight[key] = task

            def finished(done):
                self._inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()  # Consume failures even if a client disconnected.

            task.add_done_callback(finished)
        return await asyncio.shield(self._inflight[key]), False

    async def _generate_and_cache(self, key):
        async with self._slots:
            audio = await self._generate(*key)
        # Retry the online voice later instead of keeping a fallback indefinitely.
        ttl = 60 if self.engine == "auto" and audio.engine == "macos-say" else 86400
        self.cache[key] = (time.monotonic() + ttl, audio)
        self.cache_bytes += len(audio.data)
        self.cache.move_to_end(key)
        while len(self.cache) > self.cache_limit or self.cache_bytes > self.cache_byte_limit:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed[1].data)
        return audio

    async def _generate(self, text: str, accent: str, speed: str, voice: Optional[str] = None) -> Audio:
        should_try_online = self.engine == "edge" or (
            self.engine == "auto" and time.monotonic() >= self._online_retry_at
        )
        if should_try_online:
            try:
                return await asyncio.wait_for(
                    self._edge_audio(text, accent, speed, voice), timeout=self.timeout
                )
            except Exception as exc:
                logger.warning("Online speech failed (%s)", type(exc).__name__)
                if self.engine == "edge":
                    raise SpeechUnavailable("在线语音暂时不可用，请检查网络连接后重试。") from exc
                self._online_retry_at = time.monotonic() + 60
        if self.engine in {"auto", "macos"} and self.local_available:
            try:
                return await self._macos_audio(text, accent, speed, voice)
            except Exception as exc:
                logger.warning("Local speech failed (%s)", type(exc).__name__)
                raise SpeechUnavailable(
                    "系统语音暂时不可用，请确认所选声音已完整安装，或换一个本地声音重试。"
                ) from exc
        if self.engine == "macos":
            raise SpeechUnavailable("系统语音模式仅支持安装了英文语音的 macOS，请使用在线模式。")
        raise SpeechUnavailable("在线语音暂时不可用，请检查网络连接后重试。")

    async def _edge_audio(self, text: str, accent: str, speed: str, voice: Optional[str] = None) -> Audio:
        voice = voice or VOICES[accent]
        communicate = edge_tts.Communicate(text, voice, rate=RATES[speed])
        chunks = []
        size = 0
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
                size += len(chunk["data"])
                if size > 2 * 1024 * 1024:
                    raise SpeechUnavailable("返回的音频过大，请缩短输入内容。")
        data = b"".join(chunks)
        if not data:
            raise SpeechUnavailable("语音服务没有返回音频，请重试。")
        return Audio(data, "audio/mpeg", "mp3", "edge-tts", voice)

    async def _macos_audio(self, text: str, accent: str, speed: str, voice: Optional[str] = None) -> Audio:
        voice = voice or LOCAL_VOICES[accent]
        with tempfile.TemporaryDirectory(prefix="word-voice-") as directory:
            output = Path(directory) / "speech.wav"
            process = await asyncio.create_subprocess_exec(
                "say", "-v", voice, "-r", "135" if speed == "slow" else "180",
                "-o", str(output), "--file-format=WAVE", "--data-format=LEI16@22050",
                "--", text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout=30)
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if process.returncode != 0 or not output.exists():
                raise SpeechUnavailable("无法生成系统语音。")
            data = output.read_bytes()
        if len(data) <= 44 or data[:4] != b"RIFF":
            raise SpeechUnavailable("系统语音返回了无效音频。")
        with wave.open(io.BytesIO(data), "rb") as wav:
            if wav.getnframes() == 0:
                raise SpeechUnavailable("系统语音返回了空音频。")
        return Audio(data, "audio/wav", "wav", "macos-say", voice)

    async def close(self):
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
