"""Word Voice: Python API and same-origin web frontend."""

from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
from typing import Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from speech import SpeechService, SpeechUnavailable
from voices import VoiceCatalog

ROOT = Path(__file__).resolve().parent
TEXT_PATTERN = re.compile(r"[ -~\t\r\n]+\Z")
PUNCTUATION = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "\u00a0": " ",
})


class PronounceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(validation_alias=AliasChoices("text", "word"))
    accent: Literal["us", "uk"] = "us"
    speed: Literal["normal", "slow"] = "normal"
    engine: Optional[Literal["online", "local"]] = None
    voice: Optional[str] = Field(default=None, min_length=1, max_length=200)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = value.translate(PUNCTUATION)
        if not TEXT_PATTERN.fullmatch(text):
            raise ValueError("请输入英文单词或句子，可包含数字、英文标点和换行。")
        text = " ".join(text.split())
        if not 1 <= len(text) <= 1000 or not re.search(r"[A-Za-z]", text):
            raise ValueError("请输入 1–1000 个字符的英文单词或句子，至少包含一个英文字母。")
        return text


@asynccontextmanager
async def lifespan(application: FastAPI):
    timeout = float(os.environ.get("TTS_TIMEOUT", "30"))
    default_service = SpeechService(
        engine=os.environ.get("TTS_ENGINE", "auto"),
        timeout=timeout,
    )
    services = {default_service.engine: default_service}
    for engine in ("edge", "macos"):
        if engine not in services:
            services[engine] = SpeechService(engine=engine, timeout=timeout)
    application.state.speech = default_service
    application.state.speech_services = services
    application.state.voice_catalog = VoiceCatalog()
    try:
        yield
    finally:
        for service in services.values():
            await service.close()


app = FastAPI(title="英文发音", version="1.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_input(request: Request, exc: RequestValidationError):
    if any(error["loc"][-1] == "engine" for error in exc.errors()):
        return JSONResponse(status_code=422, content={
            "detail": "语音类型无效，请选择在线 TTS（online）或本地语音（local）。"
        })
    if any(error["loc"][-1] == "voice" for error in exc.errors()):
        return JSONResponse(status_code=422, content={
            "detail": "声音选项无效，请从声音列表中重新选择。"
        })
    return JSONResponse(status_code=422, content={
        "detail": "请输入不超过 1000 个字符的英文单词或句子，可包含数字、英文标点和换行，至少包含一个英文字母。口音和语速参数须有效。"
    })


@app.get("/api/health")
async def health(request: Request):
    service = request.app.state.speech
    return {"status": "ok", "engine": service.engine, "local_fallback": service.local_available}


@app.get("/api/voices")
async def voices(request: Request, engine: Literal["online", "local"] = "online"):
    try:
        return await request.app.state.voice_catalog.get(engine)
    except SpeechUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.post("/api/pronounce", responses={200: {"content": {"audio/mpeg": {}, "audio/wav": {}}}})
async def pronounce(payload: PronounceRequest, request: Request):
    if payload.voice is not None and payload.engine is None:
        return JSONResponse(status_code=422, content={
            "detail": "选择具体声音时，请同时选择在线 TTS 或本地语音。"
        })
    service = request.app.state.speech
    if payload.engine is not None:
        engine = {"online": "edge", "local": "macos"}[payload.engine]
        service = request.app.state.speech_services[engine]
    try:
        if payload.voice is not None:
            catalog = await request.app.state.voice_catalog.get(payload.engine)
            if payload.voice not in {item["id"] for item in catalog["voices"]}:
                return JSONResponse(status_code=422, content={
                    "detail": "所选声音不属于当前语音类型或已不可用，请重新加载声音列表后选择。"
                })
        audio, cached = await service.pronounce(
            payload.text, payload.accent, payload.speed, payload.voice
        )
    except SpeechUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    # Sentences can contain quotes/newlines: never copy raw input into a header.
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", payload.text).strip("-")[:48] or "speech"
    filename = "{}-{}-{}.{}".format(name, payload.accent, payload.speed, audio.extension)
    return Response(content=audio.data, media_type=audio.media_type, headers={
        "Content-Disposition": 'inline; filename="{}"'.format(filename),
        "X-Speech-Engine": audio.engine,
        "X-Speech-Voice": quote(audio.voice, safe=" -()."),
        "X-Cache": "HIT" if cached else "MISS",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
