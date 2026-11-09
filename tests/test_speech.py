"""Speech orchestration tests with deterministic fake speech providers."""

import asyncio
import io
from pathlib import Path
import struct
import time
import wave

import pytest

import speech
from speech import Audio, SpeechService, SpeechUnavailable


ONLINE = Audio(b"ID3online", "audio/mpeg", "mp3", "edge-tts", "en-US-AriaNeural")
LOCAL = Audio(b"RIFFlocal", "audio/wav", "wav", "macos-say", "Samantha")


@pytest.mark.parametrize("accent,speed,selected_voice,voice,rate", [
    ("us", "normal", None, "en-US-AriaNeural", "+0%"),
    ("us", "slow", None, "en-US-AriaNeural", "-25%"),
    ("uk", "normal", None, "en-GB-SoniaNeural", "+0%"),
    ("uk", "slow", None, "en-GB-SoniaNeural", "-25%"),
    ("us", "normal", "en-GB-RyanNeural", "en-GB-RyanNeural", "+0%"),
    ("uk", "slow", "en-AU-NatashaNeural", "en-AU-NatashaNeural", "-25%"),
])
def test_online_voice_rate_and_audio_chunk_assembly(
    monkeypatch, accent, speed, selected_voice, voice, rate
):
    seen = []
    sentence = 'Don\'t stop. Say "Hello, world!"'

    class FakeCommunicate:
        def __init__(self, text, selected_voice, rate):
            seen.append((text, selected_voice, rate))

        async def stream(self):
            yield {"type": "WordBoundary", "text": "hello"}
            yield {"type": "audio", "data": b"ID3"}
            yield {"type": "audio", "data": b"audio"}

    monkeypatch.setattr(speech.edge_tts, "Communicate", FakeCommunicate)

    async def run():
        service = SpeechService("edge")
        audio, cached = await service.pronounce(sentence, accent, speed, selected_voice)
        assert audio == Audio(b"ID3audio", "audio/mpeg", "mp3", "edge-tts", voice)
        assert cached is False

    asyncio.run(run())
    assert seen == [(sentence, voice, rate)]


@pytest.mark.parametrize("chunks", [[], [b"x" * (2 * 1024 * 1024 + 1)]])
def test_empty_or_excessive_online_audio_is_not_cached(monkeypatch, chunks):
    class FakeCommunicate:
        def __init__(self, *args, **kwargs):
            pass

        async def stream(self):
            for chunk in chunks:
                yield {"type": "audio", "data": chunk}

    monkeypatch.setattr(speech.edge_tts, "Communicate", FakeCommunicate)

    async def run():
        service = SpeechService("edge")
        with pytest.raises(SpeechUnavailable):
            await service.pronounce("hello", "us", "normal")
        assert not service.cache

    asyncio.run(run())


def test_auto_fallback_cooldown_and_recovery(monkeypatch):
    calls = []
    available = False

    async def online(self, word, accent, speed, voice=None):
        calls.append(("edge", word))
        if not available:
            raise ConnectionError("offline")
        return ONLINE

    async def local(self, word, accent, speed, voice=None):
        calls.append(("local", word))
        return LOCAL

    monkeypatch.setattr(SpeechService, "local_available", property(lambda self: True))
    monkeypatch.setattr(SpeechService, "_edge_audio", online)
    monkeypatch.setattr(SpeechService, "_macos_audio", local)

    async def run():
        nonlocal available
        service = SpeechService("auto")
        assert await service.pronounce("hello", "us", "normal") == (LOCAL, False)
        assert await service.pronounce("hello", "us", "normal") == (LOCAL, True)
        assert await service.pronounce("world", "us", "normal") == (LOCAL, False)
        assert calls == [("edge", "hello"), ("local", "hello"), ("local", "world")]
        key = ("hello", "us", "normal", None)
        assert 0 < service.cache[key][0] - time.monotonic() <= 60
        # Simulate both fallback cache and offline cooldown expiring, without sleeping.
        service.cache[key] = (time.monotonic() - 1, LOCAL)
        service._online_retry_at = 0
        available = True
        assert await service.pronounce("hello", "us", "normal") == (ONLINE, False)
        assert calls[-1] == ("edge", "hello")

    asyncio.run(run())


def test_edge_failure_does_not_use_native_voice_and_can_retry(monkeypatch):
    attempts = 0

    async def online(self, *args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("private upstream error")
        return ONLINE

    async def local(self, *args):
        pytest.fail("Explicit edge mode must not fall back")

    monkeypatch.setattr(SpeechService, "_edge_audio", online)
    monkeypatch.setattr(SpeechService, "_macos_audio", local)

    async def run():
        service = SpeechService("edge")
        with pytest.raises(SpeechUnavailable, match="在线语音暂时不可用"):
            await service.pronounce("hello", "us", "normal")
        assert not service.cache
        assert await service.pronounce("hello", "us", "normal") == (ONLINE, False)
        assert attempts == 2

    asyncio.run(run())


def test_timeout_is_reported_as_service_unavailable(monkeypatch):
    async def stalled(self, *args):
        await asyncio.Event().wait()

    monkeypatch.setattr(SpeechService, "_edge_audio", stalled)

    async def run():
        service = SpeechService("edge", timeout=0.01)
        with pytest.raises(SpeechUnavailable, match="在线语音暂时不可用"):
            await service.pronounce("hello", "us", "normal")
        assert not service.cache

    asyncio.run(run())


def test_macos_mode_on_other_platform_is_a_readable_failure(monkeypatch):
    monkeypatch.setattr(SpeechService, "local_available", property(lambda self: False))

    async def run():
        service = SpeechService("macos")
        with pytest.raises(SpeechUnavailable, match="仅支持.*macOS"):
            await service.pronounce("hello", "us", "normal")

    asyncio.run(run())


def test_concurrent_requests_share_work_and_client_cancellation_is_isolated(monkeypatch):
    async def run():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def generate(self, *args):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return ONLINE

        monkeypatch.setattr(SpeechService, "_generate", generate)
        service = SpeechService("edge")
        first = asyncio.create_task(service.pronounce("hello", "us", "normal"))
        await started.wait()
        second = asyncio.create_task(service.pronounce("hello", "us", "normal"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == (ONLINE, False)
        assert await service.pronounce("hello", "us", "normal") == (ONLINE, True)
        assert calls == 1

    asyncio.run(run())


def test_cache_evicts_least_recently_used_entry(monkeypatch):
    calls = []

    async def generate(self, word, accent, speed, voice=None):
        calls.append(word)
        return ONLINE

    monkeypatch.setattr(SpeechService, "_generate", generate)

    async def run():
        service = SpeechService("edge")
        service.cache_limit = 2
        for word in ["alpha", "bravo", "alpha", "charlie", "bravo"]:
            await service.pronounce(word, "us", "normal")
        assert calls == ["alpha", "bravo", "charlie", "bravo"]
        assert len(service.cache) == 2

    asyncio.run(run())


def test_cache_byte_limit_evicts_least_recently_used_audio(monkeypatch):
    async def generate(self, text, accent, speed, voice=None):
        return ONLINE

    monkeypatch.setattr(SpeechService, "_generate", generate)

    async def run():
        service = SpeechService("edge")
        service.cache_byte_limit = 2 * len(ONLINE.data)
        for text in ["first sentence", "second sentence", "first sentence", "third sentence"]:
            await service.pronounce(text, "us", "normal")
        assert list(service.cache) == [
            ("first sentence", "us", "normal", None), ("third sentence", "us", "normal", None)
        ]
        assert service.cache_bytes == sum(len(entry[1].data) for entry in service.cache.values())
        assert service.cache_bytes <= service.cache_byte_limit

    asyncio.run(run())


def test_expired_cache_replacement_updates_byte_total(monkeypatch):
    generated = [ONLINE, LOCAL]

    async def generate(self, text, accent, speed, voice=None):
        return generated.pop(0)

    monkeypatch.setattr(SpeechService, "_generate", generate)

    async def run():
        service = SpeechService("edge")
        key = ("A complete sentence.", "us", "normal", None)
        await service.pronounce(*key)
        service.cache[key] = (time.monotonic() - 1, ONLINE)
        assert await service.pronounce(*key) == (LOCAL, False)
        assert service.cache_bytes == len(LOCAL.data)

    asyncio.run(run())


def test_audio_larger_than_cache_budget_is_returned_without_being_cached(monkeypatch):
    async def generate(self, text, accent, speed, voice=None):
        return ONLINE

    monkeypatch.setattr(SpeechService, "_generate", generate)

    async def run():
        service = SpeechService("edge")
        service.cache_byte_limit = len(ONLINE.data) - 1
        assert await service.pronounce("A long sentence.", "us", "normal") == (ONLINE, False)
        assert not service.cache
        assert service.cache_bytes == 0

    asyncio.run(run())


def test_shutdown_cancels_pending_generation(monkeypatch):
    async def run():
        started = asyncio.Event()

        async def generate(self, *args):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(SpeechService, "_generate", generate)
        service = SpeechService("edge")
        caller = asyncio.create_task(service.pronounce("hello", "us", "normal"))
        await started.wait()
        await service.close()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not service._inflight

    asyncio.run(run())


def native_wav(frames):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(b"\0\0" * frames)
    # A metadata chunk makes a zero-frame WAV larger than the basic 44-byte header.
    # This matches the important failure mode: RIFF + nontrivial size, but no audio.
    data = output.getvalue() + b"JUNK" + struct.pack("<I", 4) + b"test"
    return data[:4] + struct.pack("<I", len(data) - 8) + data[8:]


def mock_native_process(monkeypatch, data):
    calls = []

    class FinishedProcess:
        returncode = 0

        async def communicate(self):
            return None, b""

    async def create_process(*args, **kwargs):
        calls.append(args)
        Path(args[args.index("-o") + 1]).write_bytes(data)
        return FinishedProcess()

    monkeypatch.setattr(speech.asyncio, "create_subprocess_exec", create_process)
    return calls


@pytest.mark.parametrize("accent,speed,selected_voice,voice,rate", [
    ("us", "normal", None, "Samantha", "180"),
    ("uk", "slow", None, "Daniel", "135"),
    ("us", "normal", "Eddy (英语（美国）)", "Eddy (英语（美国）)", "180"),
    ("uk", "slow", "Jamie (Premium)", "Jamie (Premium)", "135"),
])
def test_native_generation_uses_requested_voice_and_valid_wav(
    monkeypatch, accent, speed, selected_voice, voice, rate
):
    data = native_wav(128)
    calls = mock_native_process(monkeypatch, data)
    sentence = '--help; don\'t split "this sentence".'

    async def run():
        service = SpeechService("macos")
        assert await service._macos_audio(sentence, accent, speed, selected_voice) == Audio(
            data, "audio/wav", "wav", "macos-say", voice
        )

    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][:5] == ("say", "-v", voice, "-r", rate)
    assert calls[0][-2:] == ("--", sentence)
    assert "--file-format=WAVE" in calls[0]


def test_native_wav_with_metadata_but_zero_frames_is_rejected(monkeypatch):
    data = native_wav(0)
    assert len(data) > 44 and data[:4] == b"RIFF"
    mock_native_process(monkeypatch, data)

    async def run():
        service = SpeechService("macos")
        with pytest.raises(SpeechUnavailable, match="空音频"):
            await service._macos_audio("hello", "us", "normal")

    asyncio.run(run())
