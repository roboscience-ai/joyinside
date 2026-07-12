# JoyInside 机器人语音接入

将 [JoyInside](https://joy-inside.jd.com) 平台的 **Token 鉴权、ASR、TTS、智能体对话** 接入机器人语音交互。

> 默认使用 **JoyInside 平台智能体**（控制台配置人设/技能）。若需本地规则或外部 LLM，可加 `--local` 使用旧模式。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| Token 鉴权 | Access Key / Secret Key 换取短期 Token，支持自动刷新 |
| ASR | 麦克风录音 → 上传 PCM → 识别文本 |
| TTS | 文本合成 → PCM 音频 → 耳机/扬声器播放 |
| 语音对话 | `voice_chat.py`：麦克风 → ASR → **JoyInside 智能体** → TTS 播放（默认） |
| 本地模式 | `--local`：ASR + 自有逻辑/LLM + 纯 TTS（仅测链路时用） |

---

## 环境要求

- **Python 3.10+**（推荐 3.12）
- 可访问 `api.joyinside.com` 和 `ws.joyinside.com`
- 麦克风 + 耳机（语音对话）
- **FFmpeg**（可选，用于播放/转换 PCM 文件）

### Windows 快速安装

```powershell
# Python
winget install Python.Python.3.12

# FFmpeg（可选）
winget install Gyan.FFmpeg
```

安装 Python 后请**重新打开终端**，确认：

```powershell
python --version
```

若 `python` 无响应，使用完整路径：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" --version
```

---

## 一、JoyInside 控制台配置

在 [JoyInside 控制台](https://joy-inside.jd.com) 完成以下步骤（团队管理员通常只需配置一次）：

| 步骤 | 控制台位置 | 得到什么 |
|------|-----------|----------|
| 1. 注册企业 | 登录 → 提交企业信息 | `vendorId`（企业 ID） |
| 2. 创建产品型号 | 资源管理 → 型号库 | 产品型号 |
| 3. 创建应用 | 应用管理 → 新建应用 | `appId`（应用 ID） |
| 4. 获取 API 密钥 | 开发者中心 → API 密钥 | `Access Key`、`Secret Key` |
| 5. 注册设备 | 设备管理 → 新建，或运行注册脚本 | `botId`（设备 ID） |

**创建应用时注意：**

- 接入类型选择 **「语音智能体接入」**
- 在控制台配置 **人设、技能、知识库** 等，对话内容由平台智能体生成

---

## 二、获取项目与安装依赖

```powershell
git clone git@github.com:roboscience-ai/joyinside.git
cd joyinside
python -m pip install -r requirements.txt
```

---

## 三、配置 `.env`

本仓库已包含团队共用的 `.env`（含 API 密钥），克隆后**通常无需再改**，可直接使用。

若需自行配置，复制模板：

```powershell
copy .env.example .env
```

编辑 `.env`：

```ini
JOYINSIDE_ACCESS_KEY=你的AccessKey
JOYINSIDE_SECRET_KEY=你的SecretKey
JOYINSIDE_VENDOR_ID=你的企业ID
JOYINSIDE_APP_ID=你的应用ID
JOYINSIDE_BOT_ID=你的设备botId
JOYINSIDE_DEVICE_ID=robot-sn-001
```

| 环境变量 | 获取位置 | 是否必须 |
|----------|----------|----------|
| `JOYINSIDE_ACCESS_KEY` | 开发者中心 → API 密钥 | 是 |
| `JOYINSIDE_SECRET_KEY` | 开发者中心 → API 密钥 | 是 |
| `JOYINSIDE_APP_ID` | 应用管理 → 应用卡片 | 是 |
| `JOYINSIDE_BOT_ID` | 设备管理 → 设备 ID | 是 |
| `JOYINSIDE_VENDOR_ID` | 个人信息 → 企业信息 | 仅设备注册时需要 |
| `JOYINSIDE_DEVICE_ID` | 自定义设备 SN | 仅 API 注册设备时需要 |

### 注册新设备（可选）

若控制台还没有设备，可运行：

```powershell
python examples/register_device.py
```

成功后把输出的 `botId` 写入 `.env` 的 `JOYINSIDE_BOT_ID`。

---

## 四、快速验证

### 1. 测试 TTS（语音合成）

```powershell
python examples/tts_demo.py "你好，我是机器人助手"
```

成功后在项目根目录生成 `output_tts.pcm`。

播放（需 FFmpeg）：

```powershell
ffplay -f s16le -ar 16000 -ac 1 output_tts.pcm
```

或转为 WAV 后双击播放：

```powershell
ffmpeg -y -f s16le -ar 16000 -ac 1 -i output_tts.pcm output_tts.wav
```

### 2. 测试 ASR（语音识别）

准备 **16kHz、16bit、单声道** PCM 文件，例如 `test_input.pcm`：

```powershell
# 从 WAV 转换
ffmpeg -i input.wav -ar 16000 -ac 1 -f s16le test_input.pcm

python examples/asr_demo.py test_input.pcm
```

### 3. 语音对话（推荐）

默认走 **JoyInside 智能体**：录音上传 → 平台 ASR → 智能体生成回复 → 流式 TTS 播放。

| 模式 | 命令 | 说明 |
|------|------|------|
| **智能体**（默认） | `.\run_voice_chat.bat` | 使用控制台配置的人设/技能 |
| **本地大脑** | `.\run_voice_chat.bat --local` | 本地规则或外部 LLM + 纯 TTS（旧模式） |

```powershell
# JoyInside 智能体（默认）
.\run_voice_chat.bat

# 本地大脑模式
.\run_voice_chat.bat --local

# 指定麦克风/耳机
.\run_voice_chat.bat --list-devices
.\run_voice_chat.bat --input 1 --output 4
```

**流式特点：**

- 麦克风每 120ms 即上传一帧，无需等录完
- 终端实时显示 `识别中: xxx` 中间结果
- 智能体 TTS 收到首包立即播放，显示 `智能体首包 xxx ms`
- 同一会话复用 `sessionId`，多轮对话保留上下文

**使用方式：**

1. 按 **Enter** 开始录音（不是按住 Enter）
2. 对着麦克风说话，说完停顿约 **1 秒** 自动结束
3. 等待识别 → 智能体回复（文本 + 语音）
4. 说「退出」「再见」结束对话

**智能体模式实测（2026-07，国内网络）：**

| 用户说 | 智能体回复（节选） |
|--------|-------------------|
| Hello, hello | Hi there! I'm so excited to chat with you. What's your name? |
| 今天过得怎么样？ | 我今天超开心！和你聊天就像吃到了最喜欢的冰淇淋～你今天有没有遇到什么有趣的事呀？ |
| 还行吧，你怎么样？ | 我也好开心呀！和你聊天就像在公园里追泡泡一样快乐～（多轮上下文由平台 `sessionId` 维护） |

> 回复风格、人设由 JoyInside 控制台配置决定，与本地 `brain.py` 无关。

> 批处理 vs 流式延迟对比见下文「延迟实测」；`latency_benchmark.py` 主要测 **纯 TTS** 链路（`--local` 同类场景）。

### 4. 延迟实测

#### 先分清测的是什么

| 场景 | 命令 / 工具 | 测的是什么 | 典型首包延迟 |
|------|-------------|-----------|-------------|
| **纯 TTS** | `latency_benchmark.py --skip-asr` | 文本已知 → 发起合成 → 听到声音 | **~600–660 ms** |
| **本地大脑** | `.\run_voice_chat.bat --local` | ASR 完成 → 本地生成回复 → **纯 TTS** | TTS 部分 **~600–970 ms**（ASR 另计） |
| **JoyInside 智能体** | `.\run_voice_chat.bat`（默认） | 录音开始 → ASR → **平台 LLM** → TTS | **~4.7–7.0 s**（含说话 + 静音等待 + LLM） |

**所以：之前 ~600 ms 指的是「纯 TTS 首包」**——本地已有回复文本、只调用 JoyInside 合成语音时的网络延迟。  
**不是**整轮对话延迟，也 **不包含** 平台 LLM 推理时间。

#### 纯 TTS 基准（`latency_benchmark.py`）

运行基准脚本，支持 **批处理 / 流式对比**（仅 TTS，不含 LLM）：

```powershell
# 对比两种模式（推荐，主要测 TTS 感知延迟）
python examples/latency_benchmark.py --compare --rounds 2 --skip-asr

# 含 ASR 的完整对比（需真人语音 PCM）
python examples/latency_benchmark.py --compare --rounds 2 --pcm test_input.pcm

# 单独测某一种模式
python examples/latency_benchmark.py --mode streaming --rounds 3
python examples/latency_benchmark.py --mode batch --rounds 3
```

**测量指标：**

| 指标 | 含义 |
|------|------|
| Token 获取（冷/缓存） | 鉴权 HTTP 请求耗时 |
| WebSocket 连接 | 建立语音通道耗时 |
| PCM 音频配置 | 请求 TTS 下行 PCM 格式耗时 |
| ASR 上传 | 音频上传耗时 |
| ASR 识别 | 上传完成 → 收到 `IS_FINAL` |
| TTS 首包 (TTFB) | 发起合成 → 收到第一段音频 |
| TTS 全部收完 | 发起合成 → 全部音频收完 |
| **TTS 感知延迟** | 流式=首包；批处理=全部收完 |
| **对话感知延迟** | ASR 完成 → 开始听到回复（仅 `--local` 有意义） |

**纯 TTS 实测（2026-07，国内网络，约 3s 语音）：**

| 指标 | 批处理 | 流式 | 说明 |
|------|--------|------|------|
| TTS 首包 (TTFB) | ~660 ms | ~646 ms | 两种模式网络侧相近 |
| **TTS 感知延迟** | **~1606 ms** | **~646 ms** | 流式不必等全部收完，快约 1 秒 |
| WebSocket 连接 | ~1–4 s | ~1–4 s | 首次连接占比较大 |

> TTS 首包时间两种模式相同；流式的优势在于**不必等全部收完才开始播放**。

**本地大脑模式实测（`--local`，`voice_chat.py` 流式）：**

| 轮次 | ASR 识别 | TTS 首包（ASR 完成后） | TTS 全部收完 |
|------|----------|------------------------|--------------|
| 1 | Hello hello | ~971 ms | ~4211 ms |
| 2 | 今天过得怎么样？ | ~602 ms | ~7775 ms |
| 3 | （较长句） | ~725 ms | — |

本地大脑模式下，**TTS 首包仍在 ~600–970 ms**，与纯 TTS 基准一致；多出来的时间是本地 `robot_brain` 生成文本（几乎可忽略）和 ASR 识别。

**JoyInside 智能体模式实测（默认，`voice_chat.py`）：**

| 轮次 | 用户说 | 智能体首包（从按 Enter 录音算起） |
|------|--------|----------------------------------|
| 1 | Hello, hello | ~7035 ms |
| 2 | 今天过得怎么样？ | ~5491 ms |
| 3 | 还行吧，你怎么样？ | ~4718 ms |

智能体模式的「首包」包含：**你说话时长 + 静音检测（~1s）+ ASR + 平台 LLM 推理 + TTS 首包**。  
其中 LLM 推理是主要增量；若只关心「说完话后多久听到回复」，应减去录音时长和静音等待，实际等待仍明显高于纯 TTS 的 ~600 ms。

**估算一轮对话感知延迟：**

```
纯 TTS（文本已有）:     发起合成 → TTS 首包出声          ≈ 600 ms

本地大脑（--local）:    说话(ASR边录边传) → ASR完成 → TTS首包  ≈ ASR + 600 ms

智能体（默认）:         说话 → 静音等待 → ASR → 平台LLM → TTS首包  ≈ 录音 + 1s + ASR + LLM + 600 ms
```

> ASR 对比测试请用**真人说话的 PCM**（`test_input.pcm`，时长 > 1.5s）。

---

## 五、集成到自己的程序

最简示例：

```python
from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.speech import JoyInsideSpeech, SyncSpeechHelper
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.local_audio import play_pcm

cfg = JoyInsideConfig.from_env()
auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

speech = JoyInsideSpeech(
    bot_id=cfg.bot_id,
    get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
)
speech.connect()
helper = SyncSpeechHelper(speech)

# ASR
user_text = helper.recognize_and_wait(chunk_pcm(read_pcm_file("test_input.pcm")))
print("用户说:", user_text)

# 你的业务逻辑
reply = your_llm_or_rules(user_text)

# TTS（需先请求 PCM 输出，否则默认 MP3 播放会杂音）
speech.ensure_pcm_output()
audio = helper.speak_and_collect(reply)
play_pcm(audio)

speech.close()
```

完整示例见 `robot_demo.py`，实时麦克风对话见 `voice_chat.py`（默认流式）。

**流式集成示例：**

```python
from joyinside.speech import JoyInsideSpeech
from joyinside.local_audio import StreamingPcmPlayer

speech.ensure_pcm_output()
player = StreamingPcmPlayer()
player.start()

def on_tts_audio(data, meta):
    player.feed(data)

def on_tts_complete():
    player.finish()

speech.on_tts_audio = on_tts_audio
speech.on_tts_complete = on_tts_complete
speech.speak("你好")
```

**流式 ASR 集成示例：**

```python
speech.begin_asr()

def on_mic_chunk(chunk: bytes, is_last: bool):
    if chunk:
        speech.stream_asr_chunk(chunk, is_last=is_last)
    if is_last:
        speech.finish_asr()

# 在麦克风回调中调用 on_mic_chunk
```

---

## 六、项目结构

```
joyinside/
├── README.md                 # 本文档
├── 使用指南.md               # 更详细的技术说明
├── .env                      # 团队配置（含 API 密钥）
├── .env.example              # 配置模板
├── requirements.txt
├── config.py                 # 配置加载
├── voice_chat.py             # 麦克风语音对话
├── run_voice_chat.bat        # Windows 启动脚本
├── robot_demo.py             # ASR → 逻辑 → TTS 示例
├── token_service.py          # Token 中转服务（生产部署用）
├── joyinside/
│   ├── auth.py               # Token 鉴权
│   ├── device.py             # 设备注册
│   ├── speech.py             # WebSocket ASR / TTS
│   ├── audio.py              # PCM 工具
│   └── local_audio.py        # 麦克风录音 / 播放
└── examples/
    ├── register_device.py    # 注册设备
    ├── tts_demo.py           # TTS 测试
    ├── asr_demo.py           # ASR 测试
    └── latency_benchmark.py  # 延迟基准测试
```

---

## 七、常见问题

### `python` 命令无反应

Windows 上 `python` 可能指向应用商店占位程序。请：

- 使用 `.\run_voice_chat.bat`，或
- 重新安装 Python 并勾选 **Add to PATH**，或
- 使用完整路径：`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`

### TTS 播放是杂音

JoyInside 默认 TTS 返回 **MP3**，不能直接当 PCM 播放。本项目在播放前会自动调用 `ensure_pcm_output()` 请求 PCM 格式。若自行集成，务必在 TTS 前执行：

```python
speech.ensure_pcm_output()
```

### ASR 识别超时

- 录音尽量 **> 1.5 秒**，吐字清楚
- 检查麦克风设备是否正确（`--list-devices`）
- 环境吵时调低灵敏度：`.\run_voice_chat.bat --silence-threshold 0.03`
- ASR 上传按实时节奏发送，「正在识别…」等待几秒是正常现象

### 听不到播放声音

确认输出设备是否为耳机：

```powershell
.\run_voice_chat.bat --list-devices
.\run_voice_chat.bat --output <耳机设备编号>
```

### `git push` SSH 报错

若出现 `Host key verification failed`，在 push 前执行：

```powershell
$env:GIT_SSH_COMMAND = "C:/Windows/System32/OpenSSH/ssh.exe -o StrictHostKeyChecking=accept-new -i $env:USERPROFILE/.ssh/id_ed25519"
git push
```

---

## 八、音频格式

| 方向 | 格式 |
|------|------|
| ASR 输入 | PCM 16kHz / 16bit / 单声道 |
| TTS 输出 | PCM 16kHz / 16bit / 单声道（调用 `ensure_pcm_output()` 后） |

---

## 九、生产部署建议

机器人端侧**不要**保存 Secret Key。可在服务端运行 Token 中转：

```powershell
python token_service.py
```

机器人只从中转服务获取短期 Token：

```http
GET http://你的服务器:8765/token
```

详见 `使用指南.md` 第六节。

---

## 参考

- JoyInside 控制台：https://joy-inside.jd.com
- 详细技术文档：本仓库 `使用指南.md`
