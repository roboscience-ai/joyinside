# JoyInside 机器人语音接入

将 [JoyInside](https://joy-inside.jd.com) 平台的 **Token 鉴权、ASR（语音识别）、TTS（语音合成）** 接入自有机器人。

> 本项目**只使用** JoyInside 的鉴权、ASR、TTS，**不使用**其智能体对话与技能。对话逻辑由我们自己的程序负责。

---

## 功能概览

| 功能 | 说明 |
|------|------|
| Token 鉴权 | Access Key / Secret Key 换取短期 Token，支持自动刷新 |
| ASR | 麦克风录音 → 上传 PCM → 识别文本 |
| TTS | 文本合成 → PCM 音频 → 耳机/扬声器播放 |
| 语音对话 | `voice_chat.py`：麦克风说话 → 识别 → 本地逻辑回复 → 语音播放 |

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

- 接入类型选择 **「语音智能体接入」**（需要音频链路）
- 人设、技能等可保持最简配置，本项目会忽略智能体自动回复

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

Windows 推荐使用启动脚本（避免 `python` 命令指向应用商店占位程序）：

```powershell
.\run_voice_chat.bat
```

或：

```powershell
python voice_chat.py
```

**使用方式：**

1. 按 **Enter** 开始录音（不是按住 Enter）
2. 对着麦克风说话，说完停顿约 **1 秒** 自动结束
3. 等待 ASR 识别 → 机器人回复 → 耳机播放
4. 说「退出」「再见」结束对话

**指定麦克风/耳机设备：**

```powershell
.\run_voice_chat.bat --list-devices
.\run_voice_chat.bat --input 1 --output 4
```

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

完整示例见 `robot_demo.py`，实时麦克风对话见 `voice_chat.py`。

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
    └── asr_demo.py           # ASR 测试
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
