# 英文发音

输入英文单词或句子，选择在线 TTS 或本地语音，再选择具体声音，由 Python 后端生成音频，在网页中播放。页面保留输入框、语音类型、声音下拉框、生成按钮和音频播放器。

## 启动

直接使用本机已有 Conda 环境，不创建新环境：

```bash
cd /Users/tzq/Python/word-voice
conda activate base
python app.py
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。停止服务按 `Ctrl+C`。

也可运行 `./start.sh`，它优先使用已激活的 Conda 环境，否则使用现有 `base`。在其他环境首次使用时运行 `python -m pip install -r requirements.txt`。

## 使用

输入英文单词或句子，例如：

```text
Hello! How are you today?
I'm learning English, one sentence at a time.
```

选择「在线 TTS」或「本地语音」，再从「选择声音」下拉框选择声线，点击「生成并播放」，也可按 `Ctrl+Enter` 或 `Command+Enter`。生成后自动播放；如浏览器限制自动播放，点击播放器的播放按钮即可。切换类型或声音后需重新生成音频。

在线列表实时读取服务提供的英文声音，显示名称、英语地区和性别，例如 Aria、Guy、Sonia；本地列表读取运行后端的 Mac 已安装的英文声音，例如 Samantha、Daniel、Karen。声音是否出现在列表中以实际返回为准。列表加载失败时可点击「重新加载」，也可以切换到另一种语音类型。默认优先选择在线 Aria 或本地 Samantha（若已安装）。

最多输入 1000 个字符，可包含数字、英文标点及换行，至少包含一个英文字母。常见弯引号、长短横、英文省略号会统一处理，换行和连续空白合并为空格。

- 在线 TTS（默认）：通过 edge-tts 生成 MP3，无需 API Key，需要联网，输入内容会发送给在线语音服务。
- 本地语音：通过运行后端的 Mac 系统语音生成 WAV，可离线使用，不发送内容到在线语音服务。当前本地模式仅支持安装了英文语音的 macOS。

选择后只使用指定类型，生成失败会显示错误，不自动切换到另一种类型。

## 可选配置

```bash
# API 未指定 engine 时，默认使用在线语音
TTS_ENGINE=edge python app.py

# API 未指定 engine 时，默认使用 macOS 系统语音
TTS_ENGINE=macos python app.py

# 调整端口和在线超时秒数
PORT=8001 TTS_TIMEOUT=45 python app.py
```

声音选项决定实际声线和英语地区；后端接口保留慢速参数。没有指定具体声音的旧请求继续按 `accent` 使用默认美音（Aria / Samantha）或英音（Sonia / Daniel）。系统语音需要本机已安装对应声音，可用 `say -v '?'` 查看。

网页的类型选择优先于 `TTS_ENGINE`。旧客户端未传入 `engine` 时，仍使用环境配置，默认 `auto`，即在线失败后在 macOS 尝试本地语音。

不同语音模式和具体声音的音频缓存独立，每种模式最多 128 条、32 MiB，音频保留 24 小时；旧 API 自动回退的音频保留 60 秒。声音列表在后端缓存：在线 1 小时、本地 5 分钟；失败或空列表会在重试时重新读取。服务重启即清空缓存。页面不保存输入历史。

## 接口与测试

`POST /api/pronounce`：

```json
{"text": "Hello! How are you today?", "engine": "online", "voice": "en-US-AriaNeural"}
```

`engine` 为 `online`（在线 TTS）或 `local`（本地语音）。`voice` 必须使用当前引擎声音列表中的完整 `id`；指定 `voice` 时必须同时指定 `engine`。成功返回 MP3 或 WAV 音频；无效输入或声音返回 422，语音服务不可用返回 503，错误说明位于 JSON 的 `detail` 字段。兼容原来的 `word` 字段（同样支持句子），但不要同时传入 `text` 和 `word`。可选参数 `accent` 为 `us` / `uk`，`speed` 为 `normal` / `slow`；指定声音时以声音自身的地区为准。

`GET /api/voices?engine=online` 或 `GET /api/voices?engine=local` 获取具体声音，返回 `voices` 数组（包含 `id`、`label`、`locale`、`gender`）与 `default_voice`。本地声音的完整 ID 可能带有中文地区后缀，应原样传回。响应头 `X-Speech-Voice` 中的非 ASCII 字符采用 URL 百分号编码。

`GET /api/health` 检查本地服务。完整接口文档见 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

在已激活的 Conda 环境中运行：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试模拟语音服务，不需要联网。

主要文件：`app.py` 负责接口及校验，`speech.py` 负责语音与音频缓存，`voices.py` 负责读取声音列表，`static/` 是原生 HTML/CSS/JavaScript 前端。无需前端构建或 Node.js。
