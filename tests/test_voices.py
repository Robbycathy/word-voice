"""Real catalog parsing and discovery, with external providers replaced by fakes."""

import asyncio

import pytest

import voices
from speech import SpeechUnavailable
from voices import VoiceCatalog, parse_local_voices, parse_online_voices


ONLINE_ROWS = [
    {"ShortName": "en-US-AndrewNeural", "Locale": "en-US", "Gender": "Male"},
    {"ShortName": "en-US-AriaNeural", "Locale": "en-US", "Gender": "Female"},
    {"ShortName": "en-GB-SoniaNeural", "Locale": "en-GB", "Gender": "Female"},
    {"ShortName": "en-AU-NatashaNeural", "Locale": "en-AU", "Gender": "Female"},
    {"ShortName": "zh-CN-XiaoxiaoNeural", "Locale": "zh-CN", "Gender": "Female"},
]
LOCAL_OUTPUT = """Daniel              en_GB    # Hello! My name is Daniel.
Eddy (英语（美国）)   en_US    # Hello! My name is Eddy.
Eddy (英语（英国）)   en_GB    # Hello! My name is Eddy.
Samantha (英语（美国）) en_US # Hello! My name is Samantha.
Samantha (英语（美国）) en_US # Hello! My name is Samantha.
Tingting (中文（中国大陆）) zh_CN # 你好。
This line is not a voice.
"""


def test_online_parser_filters_english_rows_and_preserves_voice_ids():
    rows = ONLINE_ROWS + [ONLINE_ROWS[1], None, {}, {"Locale": "en-US", "ShortName": ""}]
    choices = parse_online_voices(rows)
    by_id = {item["id"]: item for item in choices}
    assert len(choices) == 4
    assert set(by_id) == {row["ShortName"] for row in ONLINE_ROWS if row["Locale"].startswith("en-")}
    assert by_id["en-US-AriaNeural"] == {
        "id": "en-US-AriaNeural", "label": "Aria · 美式英语 · 女声",
        "locale": "en-US", "gender": "Female",
    }


def test_local_parser_keeps_complete_installed_names_and_filters_english():
    choices = parse_local_voices(LOCAL_OUTPUT)
    by_id = {item["id"]: item for item in choices}
    assert len(choices) == 4
    assert set(by_id) == {"Daniel", "Eddy (英语（美国）)", "Eddy (英语（英国）)", "Samantha (英语（美国）)"}
    assert by_id["Eddy (英语（美国）)"]["locale"] == "en-US"
    assert by_id["Eddy (英语（英国）)"]["locale"] == "en-GB"
    assert by_id["Samantha (英语（美国）)"]["label"] == "Samantha · 美式英语"


def test_online_catalog_returns_preferred_default_and_caches_success(monkeypatch):
    calls = 0

    async def list_voices():
        nonlocal calls
        calls += 1
        return ONLINE_ROWS

    monkeypatch.setattr(voices.edge_tts, "list_voices", list_voices)

    async def run():
        catalog = VoiceCatalog()
        first = await catalog.get("online")
        second = await catalog.get("online")
        assert first == second
        assert first["engine"] == "online"
        assert first["default_voice"] == "en-US-AriaNeural"
        assert first["voices"][0]["id"] == "en-US-AndrewNeural"
        assert calls == 1

    asyncio.run(run())


def test_local_catalog_uses_say_without_network_and_caches_real_names(monkeypatch):
    commands = []

    async def forbid_network():
        pytest.fail("Local voice enumeration must not contact the online provider")

    class Process:
        returncode = 0

        async def communicate(self):
            return LOCAL_OUTPUT.encode("utf-8"), b""

    async def create_process(*args, **kwargs):
        commands.append(args)
        return Process()

    monkeypatch.setattr(voices.edge_tts, "list_voices", forbid_network)
    monkeypatch.setattr(voices.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(voices.shutil, "which", lambda program: "/usr/bin/say")
    monkeypatch.setattr(voices.asyncio, "create_subprocess_exec", create_process)

    async def run():
        catalog = VoiceCatalog()
        result = await catalog.get("local")
        assert result["engine"] == "local"
        assert result["default_voice"] == "Samantha (英语（美国）)"
        assert len(result["voices"]) == 4
        assert await catalog.get("local") == result

    asyncio.run(run())
    assert commands == [("say", "-v", "?")]


def test_failed_online_discovery_is_retried_instead_of_cached(monkeypatch):
    calls = 0

    async def list_voices():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("fake network failure")
        return ONLINE_ROWS

    monkeypatch.setattr(voices.edge_tts, "list_voices", list_voices)

    async def run():
        catalog = VoiceCatalog()
        with pytest.raises(SpeechUnavailable, match="在线声音列表"):
            await catalog.get("online")
        assert (await catalog.get("online"))["default_voice"] == "en-US-AriaNeural"
        assert calls == 2

    asyncio.run(run())


def test_empty_catalog_is_retried_and_default_falls_back_to_available_voice(monkeypatch):
    responses = [[], [ONLINE_ROWS[2]]]

    async def list_voices():
        return responses.pop(0)

    monkeypatch.setattr(voices.edge_tts, "list_voices", list_voices)

    async def run():
        catalog = VoiceCatalog()
        assert await catalog.get("online") == {"engine": "online", "voices": [], "default_voice": None}
        assert (await catalog.get("online"))["default_voice"] == "en-GB-SoniaNeural"

    asyncio.run(run())
