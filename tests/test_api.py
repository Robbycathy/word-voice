"""API contract tests; speech generation is stubbed, with no network access."""

import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import app
from speech import Audio, SpeechService, SpeechUnavailable
from voices import VoiceCatalog


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def generate(self, text, accent, speed, voice=None):
        calls.append((text, accent, speed))
        return Audio(b"ID3test-audio", "audio/mpeg", "mp3", "edge-tts", "test-voice")

    monkeypatch.setenv("TTS_ENGINE", "edge")
    monkeypatch.setattr(SpeechService, "_generate", generate)
    with TestClient(app) as instance:
        yield instance, calls


def test_audio_response_is_cached_and_describes_actual_voice(client):
    http, calls = client
    payload = {"text": "hello", "accent": "uk", "speed": "slow"}
    first = http.post("/api/pronounce", json=payload)
    second = http.post("/api/pronounce", json=payload)

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"ID3test-audio"
    assert first.headers["content-type"] == "audio/mpeg"
    assert first.headers["x-speech-engine"] == "edge-tts"
    assert first.headers["x-speech-voice"] == "test-voice"
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert first.headers["content-disposition"] == 'inline; filename="hello-uk-slow.mp3"'
    assert calls == [("hello", "uk", "slow")]


def test_sentence_normalization_and_default_options(client):
    http, calls = client
    response = http.post("/api/pronounce", json={
        "text": " \t She said, “Don’t wait”…\r\n— go now – please.  "
    })
    assert response.status_code == 200
    assert calls == [('She said, "Don\'t wait"... - go now - please.', "us", "normal")]


def test_legacy_word_alias_and_normalized_text_share_cached_audio(client):
    http, calls = client
    legacy = http.post("/api/pronounce", json={"word": "  don’t  "})
    current = http.post("/api/pronounce", json={"text": "don't"})
    assert legacy.status_code == current.status_code == 200
    assert current.headers["x-cache"] == "HIT"
    assert calls == [("don't", "us", "normal")]


@pytest.mark.parametrize("text", [
    "Hello world! How are you today?", "I have 123 apples.", "--help",
    "word;touch /tmp/file", "<script>alert('hello')</script>",
    "double--dash", "trailing-", "Email me at hello@example.com.",
    "Printable:" + "".join(chr(code) for code in range(32, 127)),
])
def test_printable_english_sentences_are_passed_to_speech_unchanged(client, text):
    http, calls = client
    response = http.post("/api/pronounce", json={"text": text})
    assert response.status_code == 200
    assert calls == [(text, "us", "normal")]


def test_sentence_download_filename_is_short_and_cannot_inject_headers(client):
    http, calls = client
    text = 'Hello "friend"!\r\nX-Injected: value; /../' + "longsentence" * 10
    response = http.post("/api/pronounce", json={"text": text})
    assert response.status_code == 200
    assert calls == [(text.replace("\r\n", " "), "us", "normal")]
    assert re.fullmatch(
        r'inline; filename="[A-Za-z0-9_-]{1,48}-us-normal\.mp3"',
        response.headers["content-disposition"],
    )
    assert "x-injected" not in response.headers


def test_length_boundary_is_applied_after_whitespace_normalization(client):
    http, calls = client
    text = "a" * 1000
    response = http.post("/api/pronounce", json={"text": " \r\n\t" + text + "  "})
    assert response.status_code == 200
    assert calls == [(text, "us", "normal")]


def test_newline_and_tab_sequences_collapse_to_one_space(client):
    http, calls = client
    response = http.post("/api/pronounce", json={"text": "Hello\n\r\n\t  world.\tYes!"})
    assert response.status_code == 200
    assert calls == [("Hello world. Yes!", "us", "normal")]


def test_nonbreaking_spaces_and_left_curly_apostrophes_are_normalized(client):
    http, calls = client
    response = http.post("/api/pronounce", json={"text": "I\u00a0\u00a0don‘t mind."})
    assert response.status_code == 200
    assert calls == [("I don't mind.", "us", "normal")]


def test_case_is_preserved_and_accent_speed_have_separate_cache_entries(client):
    http, calls = client
    for payload in [
        {"text": "US"}, {"text": "us"},
        {"text": "US", "accent": "uk"},
        {"text": "US", "speed": "slow"},
    ]:
        assert http.post("/api/pronounce", json=payload).status_code == 200
    assert calls == [
        ("US", "us", "normal"), ("us", "us", "normal"),
        ("US", "uk", "normal"), ("US", "us", "slow"),
    ]


@pytest.mark.parametrize("payload", [
    {}, {"text": ""}, {"text": " \n\t "}, {"text": "12345"},
    {"text": "!@#$%^&*()"}, {"text": "你好"}, {"text": "Hello 你好"},
    {"text": "café"}, {"text": "Hello 🙂"}, {"text": "x" * 1001},
    {"text": "Hello\0world"}, {"text": "Hello\x1bworld"},
    {"text": "Hello\x7fworld"}, {"text": "Hello\u200bworld"},
    {"text": "Hello\vworld"}, {"text": "Hello\fworld"},
    {"text": "Hello\x1cworld"},
    {"text": None}, {"text": 123}, {"text": ["hello"]},
    {"text": "hello", "accent": "fr"},
    {"text": "hello", "speed": "fast"},
    {"text": "hello", "voice": "unapproved-voice"},
    {"text": "hello", "word": "hello"},
    {"text": "hello", "engine": "unknown"},
    {"text": "hello", "engine": "auto"},
    {"text": "hello", "engine": "edge"},
    {"text": "hello", "engine": ["online"]},
    {"text": "hello", "engine": "online", "voice": ""},
    {"text": "hello", "engine": "local", "voice": "x" * 201},
    {"word": "x" * 1001},
])
def test_invalid_requests_never_reach_speech_service(client, payload):
    http, calls = client
    response = http.post("/api/pronounce", json=payload)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    assert calls == []


def test_malformed_json_has_readable_error(client):
    http, calls = client
    response = http.post(
        "/api/pronounce", content='{"text":', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "detail" in response.json()
    assert calls == []


def test_service_error_is_exposed_as_503(client, monkeypatch):
    http, calls = client

    async def unavailable(self, text, accent, speed, voice=None):
        raise SpeechUnavailable("在线语音暂时不可用，请检查网络连接后重试。")

    monkeypatch.setattr(SpeechService, "_generate", unavailable)
    response = http.post("/api/pronounce", json={"text": "hello"})
    assert response.status_code == 503
    assert response.json() == {"detail": "在线语音暂时不可用，请检查网络连接后重试。"}


def test_native_audio_uses_wav_response_and_filename(client, monkeypatch):
    http, _ = client

    async def native(self, text, accent, speed, voice=None):
        return Audio(b"RIFFnative-audio", "audio/wav", "wav", "macos-say", "Daniel")

    monkeypatch.setattr(SpeechService, "_generate", native)
    response = http.post("/api/pronounce", json={"text": "colour", "accent": "uk"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-speech-engine"] == "macos-say"
    assert response.headers["x-speech-voice"] == "Daniel"
    assert response.headers["content-disposition"].endswith('colour-uk-normal.wav"')


def test_health_reports_configuration_without_synthesizing(client):
    http, calls = client
    response = http.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["engine"] == "edge"
    assert isinstance(response.json()["local_fallback"], bool)
    assert calls == []


@pytest.fixture
def engine_providers(monkeypatch):
    """Keep real engine selection/fallback logic, replacing only audio providers."""
    calls = []

    async def online(self, text, accent, speed, voice=None):
        calls.append(("edge", text, accent, speed))
        return Audio(b"ID3online", "audio/mpeg", "mp3", "edge-tts", "en-US-AriaNeural")

    async def local(self, text, accent, speed, voice=None):
        calls.append(("macos", text, accent, speed))
        return Audio(b"RIFFlocal", "audio/wav", "wav", "macos-say", "Samantha")

    monkeypatch.setattr(SpeechService, "_edge_audio", online)
    monkeypatch.setattr(SpeechService, "_macos_audio", local)
    monkeypatch.setattr(SpeechService, "local_available", property(lambda self: True))
    return calls


def test_selected_engines_return_correct_audio_and_keep_separate_caches(monkeypatch, engine_providers):
    monkeypatch.setenv("TTS_ENGINE", "auto")
    with TestClient(app) as http:
        for cache_status in ["MISS", "HIT"]:
            for engine, mime, header, voice, content in [
                ("online", "audio/mpeg", "edge-tts", "en-US-AriaNeural", b"ID3online"),
                ("local", "audio/wav", "macos-say", "Samantha", b"RIFFlocal"),
            ]:
                response = http.post("/api/pronounce", json={
                    "text": "The same sentence.", "engine": engine
                })
                assert response.status_code == 200
                assert response.content == content
                assert response.headers["content-type"] == mime
                assert response.headers["x-speech-engine"] == header
                assert response.headers["x-speech-voice"] == voice
                assert response.headers["x-cache"] == cache_status
    assert engine_providers == [
        ("edge", "The same sentence.", "us", "normal"),
        ("macos", "The same sentence.", "us", "normal"),
    ]


@pytest.mark.parametrize("default,selected,provider,header", [
    ("macos", "online", "edge", "edge-tts"),
    ("edge", "local", "macos", "macos-say"),
])
def test_explicit_engine_overrides_environment_without_calling_other_provider(
    monkeypatch, engine_providers, default, selected, provider, header
):
    monkeypatch.setenv("TTS_ENGINE", default)
    with TestClient(app) as http:
        response = http.post("/api/pronounce", json={"text": "Hello.", "engine": selected})
        assert response.status_code == 200
        assert response.headers["x-speech-engine"] == header
    assert engine_providers == [(provider, "Hello.", "us", "normal")]


@pytest.mark.parametrize("default,header", [("edge", "edge-tts"), ("macos", "macos-say")])
def test_omitted_engine_keeps_environment_default(monkeypatch, engine_providers, default, header):
    monkeypatch.setenv("TTS_ENGINE", default)
    with TestClient(app) as http:
        response = http.post("/api/pronounce", json={"text": "Hello."})
        assert response.status_code == 200
        assert response.headers["x-speech-engine"] == header
    assert engine_providers == [(default, "Hello.", "us", "normal")]


def test_explicit_online_failure_does_not_reuse_fallback_cache_or_call_local(
    monkeypatch, engine_providers
):
    monkeypatch.setenv("TTS_ENGINE", "auto")

    async def unavailable(self, text, accent, speed, voice=None):
        engine_providers.append(("edge", text, accent, speed))
        raise ConnectionError("fake offline provider")

    monkeypatch.setattr(SpeechService, "_edge_audio", unavailable)
    with TestClient(app) as http:
        # Default auto mode may fall back, but an explicit online choice must not.
        default = http.post("/api/pronounce", json={"text": "Hello."})
        assert default.status_code == 200
        assert default.headers["x-speech-engine"] == "macos-say"
        online = http.post("/api/pronounce", json={"text": "Hello.", "engine": "online"})
        assert online.status_code == 503
        assert "在线语音暂时不可用" in online.json()["detail"]
    assert engine_providers == [
        ("edge", "Hello.", "us", "normal"),
        ("macos", "Hello.", "us", "normal"),
        ("edge", "Hello.", "us", "normal"),
    ]


def test_shutdown_closes_each_engine_service_once(monkeypatch, engine_providers):
    monkeypatch.setenv("TTS_ENGINE", "auto")
    closed = []
    original_close = SpeechService.close

    async def close(self):
        closed.append(id(self))
        await original_close(self)

    monkeypatch.setattr(SpeechService, "close", close)
    with TestClient(app):
        services = list(app.state.speech_services.values())
        service_ids = {id(service) for service in services}
        assert {service.engine for service in services} == {"auto", "edge", "macos"}
    assert set(closed) == service_ids
    assert len(closed) == len(service_ids)


@pytest.fixture
def voice_client(monkeypatch):
    catalogues = {
        "online": {
            "engine": "online", "default_voice": "en-US-AriaNeural",
            "voices": [
                {"id": "en-US-AriaNeural", "label": "Aria", "locale": "en-US", "gender": "Female"},
                {"id": "en-GB-RyanNeural", "label": "Ryan", "locale": "en-GB", "gender": "Male"},
            ],
        },
        "local": {
            "engine": "local", "default_voice": "Samantha (英语（美国）)",
            "voices": [
                {"id": "Samantha (英语（美国）)", "label": "Samantha", "locale": "en-US", "gender": ""},
                {"id": "Daniel", "label": "Daniel", "locale": "en-GB", "gender": ""},
            ],
        },
    }
    generated = []

    async def catalogue(self, engine):
        return catalogues[engine]

    async def generate(self, text, accent, speed, voice=None):
        generated.append((self.engine, text, voice))
        local = self.engine == "macos"
        return Audio(
            voice.encode("utf-8"), "audio/wav" if local else "audio/mpeg",
            "wav" if local else "mp3", "macos-say" if local else "edge-tts", voice,
        )

    monkeypatch.setenv("TTS_ENGINE", "auto")
    monkeypatch.setattr(VoiceCatalog, "get", catalogue)
    monkeypatch.setattr(SpeechService, "_generate", generate)
    with TestClient(app) as http:
        yield http, generated, catalogues


@pytest.mark.parametrize("engine", ["online", "local"])
def test_voice_list_endpoint_returns_provider_choices_and_default(voice_client, engine):
    http, generated, catalogues = voice_client
    response = http.get("/api/voices", params={"engine": engine})
    assert response.status_code == 200
    assert response.json() == catalogues[engine]
    assert generated == []


def test_voice_list_rejects_unknown_engine(voice_client):
    http, generated, _ = voice_client
    response = http.get("/api/voices", params={"engine": "unknown"})
    assert response.status_code == 422
    assert generated == []


def test_selected_voices_have_separate_audio_cache_and_encoded_headers(voice_client):
    http, generated, _ = voice_client
    choices = [
        ("online", "en-US-AriaNeural"),
        ("online", "en-GB-RyanNeural"),
        ("local", "Samantha (英语（美国）)"),
        ("local", "Daniel"),
    ]
    for cache_status in ["MISS", "HIT"]:
        for engine, voice in choices:
            response = http.post("/api/pronounce", json={
                "text": "Same words, different voices.", "engine": engine, "voice": voice
            })
            assert response.status_code == 200
            assert response.content == voice.encode("utf-8")
            assert response.headers["x-cache"] == cache_status
            assert response.headers["x-speech-voice"].isascii()
            assert unquote(response.headers["x-speech-voice"]) == voice
            if voice.isascii():
                assert response.headers["x-speech-voice"] == voice
    assert generated == [
        ("edge" if engine == "online" else "macos", "Same words, different voices.", voice)
        for engine, voice in choices
    ]


@pytest.mark.parametrize("engine,voice", [
    ("online", "unknown-online-voice"), ("local", "unknown-local-voice"),
    ("online", "Daniel"), ("local", "en-US-AriaNeural"),
])
def test_unknown_or_other_engine_voice_never_reaches_provider(voice_client, engine, voice):
    http, generated, _ = voice_client
    response = http.post("/api/pronounce", json={"text": "Hello.", "engine": engine, "voice": voice})
    assert response.status_code == 422
    assert generated == []


@pytest.mark.parametrize("operation", ["list", "pronounce"])
def test_voice_catalog_failure_returns_503_and_request_can_retry(voice_client, monkeypatch, operation):
    http, generated, catalogues = voice_client
    attempts = 0

    async def catalogue(self, engine):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SpeechUnavailable("无法加载在线声音列表，请检查网络后重试。")
        return catalogues[engine]

    monkeypatch.setattr(VoiceCatalog, "get", catalogue)
    if operation == "list":
        request = lambda: http.get("/api/voices", params={"engine": "online"})
    else:
        request = lambda: http.post("/api/pronounce", json={
            "text": "Hello.", "engine": "online", "voice": "en-US-AriaNeural"
        })
    response = request()
    assert response.status_code == 503
    assert generated == []
    assert request().status_code == 200
    assert attempts == 2
