"""
单独测量 JoyInside 智能体 LLM 延迟（剥离 ASR / 录音）。

用法:
  python examples/llm_latency_benchmark.py
  python examples/llm_latency_benchmark.py --rounds 3
  python examples/llm_latency_benchmark.py --mode audio   # 音频链路触发（对照）
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.speech import JoyInsideSpeech

TEST_TEXTS = [
    "你好",
    "今天天气怎么样？",
    "Hello, how are you?",
]


@dataclass
class LlmLatency:
    mode: str
    prompt: str
    text_sent_ms: float = 0.0
    agent_start_ms: float = 0.0
    first_agent_ms: float = 0.0
    first_tts_ms: float = 0.0
    complete_ms: float = 0.0
    agent_text: str = ""
    error: str = ""

    @property
    def llm_ttfb_ms(self) -> float:
        """发起调用 → 智能体首字（CALL_AGENT_START 或首条 AGENT）。"""
        if self.agent_start_ms > 0:
            return self.agent_start_ms
        return self.first_agent_ms

    @property
    def llm_stream_ms(self) -> float:
        """发起调用 → 首条 AGENT 文本。"""
        return self.first_agent_ms

    @property
    def tts_ttfb_ms(self) -> float:
        return self.first_tts_ms


def _make_speech(cfg: JoyInsideConfig, auth: JoyInsideAuth, *, manual_mode: bool = False) -> JoyInsideSpeech:
    return JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
        use_agent=True,
        manual_mode=manual_mode,
    )


def measure_text_llm(
    speech: JoyInsideSpeech,
    text: str,
    *,
    timeout: float = 60.0,
) -> LlmLatency:
    """纯 TEXT 触发 LLM，不测 ASR。"""
    result = LlmLatency(mode="TEXT", prompt=text)
    agent_parts: list[str] = []
    done = threading.Event()
    t0 = time.perf_counter()

    def mark(name: str) -> None:
        setattr(result, f"{name}_ms", (time.perf_counter() - t0) * 1000)

    def on_agent_start(_text: str, _meta: dict) -> None:
        if result.agent_start_ms == 0.0:
            mark("agent_start")

    def on_agent(chunk: str, _meta: dict) -> None:
        agent_parts.append(chunk)
        if result.first_agent_ms == 0.0:
            mark("first_agent")

    def on_tts(_data: bytes, _meta: dict) -> None:
        if result.first_tts_ms == 0.0:
            mark("first_tts")

    def on_complete() -> None:
        mark("complete")
        done.set()

    speech.on_agent_start = on_agent_start
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts
    speech.on_round_complete = on_complete

    try:
        speech.send_text(text)
        mark("text_sent")
        if not done.wait(timeout):
            result.error = "超时"
    except Exception as exc:
        result.error = str(exc)
    finally:
        result.agent_text = "".join(agent_parts).strip()
        speech.on_agent_start = None
        speech.on_agent = None
        speech.on_tts_audio = None
        speech.on_round_complete = None

    return result


def measure_audio_llm(
    speech: JoyInsideSpeech,
    pcm_path: Path,
    *,
    timeout: float = 90.0,
) -> LlmLatency:
    """音频上传 + CLIENT_AUDIO_FINISH，等平台自动走智能体（对照组）。"""
    result = LlmLatency(mode="AUDIO", prompt=pcm_path.name)
    agent_parts: list[str] = []
    asr_text = ""
    done = threading.Event()
    audio_finish_at = 0.0
    t0 = time.perf_counter()

    def mark_from(base: float, name: str) -> None:
        setattr(result, f"{name}_ms", (time.perf_counter() - base) * 1000)

    def on_asr_final(text: str) -> None:
        nonlocal asr_text
        asr_text = text.strip()
        if result.text_sent_ms == 0.0:
            result.text_sent_ms = (time.perf_counter() - t0) * 1000

    def on_agent_start(_text: str, _meta: dict) -> None:
        if result.agent_start_ms == 0.0 and audio_finish_at:
            mark_from(audio_finish_at, "agent_start")

    def on_agent(chunk: str, _meta: dict) -> None:
        agent_parts.append(chunk)
        if result.first_agent_ms == 0.0 and audio_finish_at:
            mark_from(audio_finish_at, "first_agent")

    def on_tts(_data: bytes, _meta: dict) -> None:
        if result.first_tts_ms == 0.0 and audio_finish_at:
            mark_from(audio_finish_at, "first_tts")

    def on_complete() -> None:
        if audio_finish_at:
            mark_from(audio_finish_at, "complete")
        done.set()

    speech.on_asr_final = on_asr_final
    speech.on_agent_start = on_agent_start
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts
    speech.on_round_complete = on_complete

    try:
        speech.begin_asr()
        chunks = chunk_pcm(read_pcm_file(pcm_path))
        for i, chunk in enumerate(chunks):
            speech.stream_asr_chunk(chunk, is_last=(i == len(chunks) - 1), pace=True)
        speech.finish_asr()
        audio_finish_at = time.perf_counter()
        result.prompt = asr_text or pcm_path.name
        if not done.wait(timeout):
            result.error = "超时"
    except Exception as exc:
        result.error = str(exc)
    finally:
        result.agent_text = "".join(agent_parts).strip()
        speech.on_asr_final = None
        speech.on_agent_start = None
        speech.on_agent = None
        speech.on_tts_audio = None
        speech.on_round_complete = None

    return result


def print_result(r: LlmLatency) -> None:
    if r.error:
        print(f"  [{r.mode}] {r.prompt!r} 失败: {r.error}")
        return
    print(
        f"  [{r.mode}] {r.prompt!r}\n"
        f"    LLM 启动(TTFB): {r.llm_ttfb_ms:.0f} ms\n"
        f"    首条 AGENT:     {r.llm_stream_ms:.0f} ms\n"
        f"    TTS 首包:       {r.tts_ttfb_ms:.0f} ms\n"
        f"    整轮 COMPLETE:  {r.complete_ms:.0f} ms\n"
        f"    回复: {r.agent_text[:80]}{'…' if len(r.agent_text) > 80 else ''}"
    )


def print_summary(results: list[LlmLatency]) -> None:
    ok = [r for r in results if not r.error]
    if not ok:
        print("\n无有效样本")
        return

    def stats(vals: list[float]) -> str:
        if not vals:
            return "n/a"
        return f"avg={statistics.mean(vals):.0f}  min={min(vals):.0f}  max={max(vals):.0f}"

    print("\n" + "=" * 60)
    print("LLM 延迟汇总")
    print("=" * 60)
    for mode in sorted({r.mode for r in ok}):
        subset = [r for r in ok if r.mode == mode]
        print(f"\n{mode} 模式 (n={len(subset)})")
        print(f"  LLM 启动:  {stats([r.llm_ttfb_ms for r in subset if r.llm_ttfb_ms > 0])}")
        print(f"  首条 AGENT:{stats([r.llm_stream_ms for r in subset if r.llm_stream_ms > 0])}")
        print(f"  TTS 首包:  {stats([r.tts_ttfb_ms for r in subset if r.tts_ttfb_ms > 0])}")
        print(f"  整轮完成:  {stats([r.complete_ms for r in subset if r.complete_ms > 0])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside LLM 延迟基准")
    parser.add_argument("--rounds", type=int, default=2, help="每种模式测试轮数")
    parser.add_argument(
        "--mode",
        choices=("text", "audio", "both"),
        default="both",
        help="text=纯TEXT触发LLM; audio=音频链路; both=都测",
    )
    parser.add_argument("--pcm", default=str(ROOT / "output_tts.pcm"))
    args = parser.parse_args()

    pcm_path = Path(args.pcm)
    if args.mode in ("audio", "both") and not pcm_path.exists():
        print(f"PCM 不存在: {pcm_path}")
        sys.exit(1)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    print("=" * 60)
    print("JoyInside LLM 延迟测试")
    print("说明: 请先关闭 voice_chat.py，避免 REPEAT_CLIENT_SESSION")
    print("=" * 60)

    results: list[LlmLatency] = []

    if args.mode in ("text", "both"):
        print(f"\n--- TEXT 触发 LLM ({args.rounds} 轮) ---")
        for i in range(args.rounds):
            text = TEST_TEXTS[i % len(TEST_TEXTS)]
            speech = _make_speech(cfg, auth)
            try:
                speech.connect(session_id=str(uuid.uuid4()))
                speech.ensure_pcm_output()
                if i > 0:
                    time.sleep(1.0)
                r = measure_text_llm(speech, text)
                results.append(r)
                print_result(r)
            finally:
                speech.close()
            time.sleep(0.5)

    if args.mode in ("audio", "both"):
        print(f"\n--- 音频链路触发 LLM ({args.rounds} 轮, {pcm_path.name}) ---")
        for i in range(args.rounds):
            speech = _make_speech(cfg, auth, manual_mode=True)
                results.append(r)
                print_result(r)
            finally:
                speech.close()
            time.sleep(0.5)

    print_summary(results)


if __name__ == "__main__":
    main()
