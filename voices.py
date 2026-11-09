"""Discover real English voice choices for the online and local providers."""

import asyncio
import platform
import re
import shutil
import time

import edge_tts

from speech import SpeechUnavailable


REGIONS = {
    "en-US": "美式英语", "en-GB": "英式英语", "en-AU": "澳大利亚英语",
    "en-CA": "加拿大英语", "en-IE": "爱尔兰英语", "en-IN": "印度英语",
    "en-ZA": "南非英语", "en-NZ": "新西兰英语", "en-SG": "新加坡英语",
    "en-HK": "香港英语", "en-KE": "肯尼亚英语", "en-NG": "尼日利亚英语",
    "en-PH": "菲律宾英语", "en-TZ": "坦桑尼亚英语",
}


def _sort(voices):
    return sorted(voices, key=lambda item: (
        {"en-US": 0, "en-GB": 1}.get(item["locale"], 2),
        item["locale"], item["label"], item["id"],
    ))


def parse_online_voices(rows):
    voices = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        locale = row.get("Locale", "")
        voice_id = row.get("ShortName", "")
        if not isinstance(locale, str) or not locale.startswith("en-"):
            continue
        if not isinstance(voice_id, str) or not voice_id or len(voice_id) > 200:
            continue
        name = voice_id.removeprefix(locale + "-").removesuffix("Neural")
        gender = row.get("Gender", "")
        if not isinstance(gender, str):
            gender = ""
        gender_label = {"Female": "女声", "Male": "男声"}.get(gender, "")
        label = " · ".join(part for part in (name, REGIONS.get(locale, locale), gender_label) if part)
        voices[voice_id] = {
            "id": voice_id, "label": label, "locale": locale,
            "gender": gender,
        }
    return _sort(voices.values())


def parse_local_voices(output):
    voices = {}
    for line in output.splitlines():
        match = re.match(r"^(.+?)\s+(en_[A-Za-z0-9_]+)\s+#", line)
        if not match:
            continue
        name = match[1].strip()
        if not name or len(name) > 200:
            continue
        locale = match[2].replace("_", "-")
        # Keep the exact installed name for say; shorten only the display label.
        display = re.sub(r"\s*\([^)]*[\u3400-\u9fff][^)]*\)", "", name).strip()
        voices[name] = {
            "id": name, "label": "{} · {}".format(display, REGIONS.get(locale, locale)),
            "locale": locale, "gender": "",
        }
    return _sort(voices.values())


class VoiceCatalog:
    def __init__(self, timeout=15.0):
        self.timeout = timeout
        self._cache = {}
        self._locks = {"online": asyncio.Lock(), "local": asyncio.Lock()}

    async def get(self, engine):
        if engine not in self._locks:
            raise ValueError("Unknown voice engine")
        async with self._locks[engine]:
            cached = self._cache.get(engine)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            if engine == "online":
                try:
                    rows = await asyncio.wait_for(edge_tts.list_voices(), timeout=self.timeout)
                    choices = parse_online_voices(rows)
                except Exception as exc:
                    raise SpeechUnavailable("无法加载在线声音列表，请检查网络后重试，或选择本地语音。") from exc
                default = next((v["id"] for v in choices if v["id"] == "en-US-AriaNeural"), None)
            else:
                choices = await self._local_voices()
                default = next((v["id"] for v in choices
                                if v["locale"] == "en-US" and v["id"].split(" (")[0] == "Samantha"), None)
            result = {
                "engine": engine, "voices": choices,
                "default_voice": default or (choices[0]["id"] if choices else None),
            }
            # Empty or failed discovery is retried, so newly installed voices can appear.
            if choices:
                self._cache[engine] = (time.monotonic() + (3600 if engine == "online" else 300), result)
            return result

    async def _local_voices(self):
        if platform.system() != "Darwin" or shutil.which("say") is None:
            raise SpeechUnavailable("本地语音仅支持安装了英文声音的 macOS，请选择在线 TTS。")
        try:
            process = await asyncio.create_subprocess_exec(
                "say", "-v", "?", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            finally:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if process.returncode != 0:
                raise SpeechUnavailable("系统未能返回声音列表。")
            return parse_local_voices(stdout.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise SpeechUnavailable("无法读取本地声音列表，请检查 Mac 系统语音设置后重试。") from exc
