"""对话大脑：优先 LLM，无密钥时用自然口语规则兜底。"""

from __future__ import annotations

import logging
import os
import random
import re
from collections import deque

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「小乐」，一个自然、友好的中文语音助手。用户通过麦克风和你对话，你的文字会被合成为语音播放。

回复要求：
- 口语化、简短，每次 1～3 句话，适合朗读
- 直接回应用户内容，像朋友聊天一样自然
- 不要提「演示」「测试」「机器人框架」「大模型」等技术词
- 不知道的事就说不知道，不要编造事实
- 结合对话历史保持连贯"""

FALLBACK_REPLIES = {
    "greeting": [
        "你好呀，我在呢。想聊点什么？",
        "嗨，很高兴听到你的声音。今天怎么样？",
    ],
    "how_are_you": [
        "我挺好的，谢谢关心。你那边呢？",
        "还不错呀。你今天过得顺利吗？",
    ],
    "thanks": [
        "不客气，有需要随时说。",
        "别客气，我很乐意陪你聊。",
    ],
    "bye": [
        "好的，下次再聊，再见。",
        "嗯，拜拜，照顾好自己。",
    ],
    "name": [
        "我叫小乐，是你的语音助手。你可以把我当朋友一样聊天。",
    ],
    "capability": [
        "我可以陪你聊天、听你说话、回答一些日常问题。虽然我不能查实时天气或新闻，但闲聊没问题。",
    ],
    "unknown": [
        "嗯，这个话题我一下子想不到特别好的回答。你能换个说法，或者多说一点吗？",
        "有意思。我对这个了解不多，不过我很想听你怎么想的。",
        "哈哈，这个我得想想。你为什么会问这个？",
    ],
}


class ConversationBrain:
    """多轮对话大脑，支持 OpenAI 兼容 API。"""

    def __init__(self) -> None:
        self._history: deque[dict[str, str]] = deque(maxlen=20)
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    @property
    def uses_llm(self) -> bool:
        return bool(self.api_key)

    def reply(self, user_text: str) -> str:
        text = user_text.strip().lstrip("，, ")
        if not text:
            return "我没太听清，方便再说一遍吗？"

        if self.uses_llm:
            try:
                answer = self._llm_reply(text)
            except Exception as exc:
                logger.warning("LLM 调用失败，使用本地回复: %s", exc)
                answer = self._local_reply(text)
        else:
            answer = self._local_reply(text)

        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": answer})
        return answer

    def _llm_reply(self, user_text: str) -> str:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self._history)
        messages.append({"role": "user", "content": user_text})

        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0.8,
                "max_tokens": 200,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return self._trim_for_speech(content)

    def _local_reply(self, text: str) -> str:
        lower = text.lower()

        if any(w in lower for w in ("你好", "您好", "hello", "hi", "嗨")):
            return _pick("greeting")
        if re.search(r"怎么样|如何|还好吗|过得怎样", text):
            return _pick("how_are_you")
        if any(w in text for w in ("谢谢", "感谢", "多谢")):
            return _pick("thanks")
        if any(w in text for w in ("再见", "拜拜", "回见", "下次聊")):
            return _pick("bye")
        if re.search(r"叫什么|名字|你是谁", text):
            return _pick("name")
        if re.search(r"能做什么|会什么|有什么用|能干吗", text):
            return _pick("capability")

        # 结合上一轮做简单连贯
        last_user = _last_user_message(self._history)
        if last_user and _is_follow_up(text):
            return _follow_up_reply(last_user, text)

        return _pick("unknown")

    @staticmethod
    def _trim_for_speech(text: str) -> str:
        text = text.strip().strip('"').strip("'")
        # 语音不宜过长，取前两段
        parts = re.split(r"\n+", text)
        if len(parts) > 2:
            text = "\n".join(parts[:2])
        if len(text) > 180:
            text = text[:180].rsplit("。", 1)[0] + "。"
        return text or "嗯，我在听，你继续说。"


def _pick(key: str) -> str:
    return random.choice(FALLBACK_REPLIES[key])


def _last_user_message(history: deque[dict[str, str]]) -> str:
    for msg in reversed(history):
        if msg["role"] == "user":
            return msg["content"]
    return ""


def _is_follow_up(text: str) -> bool:
    return bool(
        re.search(
            r"^(那|然后|所以|为什么|怎么|真的|是吗|对啊|嗯|好|可以|行|继续)",
            text,
        )
        or len(text) <= 8
    )


def _follow_up_reply(last_user: str, text: str) -> str:
    if "你好" in last_user or "嗨" in last_user:
        return "嗯，我在听。你想聊工作、生活，还是随便聊聊都行。"
    if "怎么样" in last_user:
        return "听起来不错。还有什么想分享的吗？"
    return "我明白你的意思。然后呢，你还想聊点什么？"
