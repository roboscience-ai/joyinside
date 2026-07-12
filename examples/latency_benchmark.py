"""
JoyInside 延迟基准测试。

测量 Token、WebSocket 连接、ASR、TTS 各阶段耗时。

用法:
  python examples/latency_benchmark.py
  python examples/latency_benchmark.py --pcm output_tts.pcm --rounds 3
  python examples/latency_benchmark.py --tts-text "你好" --skip-asr
"""

from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import SAMPLE_RATE, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.speech import JoyInsideSpeech

TTS_TEXT_DEFAULT = "你好，我是语音助手，有什么可以帮你的？"


@dataclass
class LatencyResult:
    token_cold_ms: float = 0.0
    token_cached_ms: float = 0.0
    ws_connect_ms: float = 0.0
    pcm_config_ms: float = 0.0
    asr_upload_ms: float = 0.0
    asr_recognition_ms: float = 0.0
    asr_total_ms: float = 0.0
    asr_text: str = ""
    tts_first_audio_ms: float = 0.0
    tts_complete_ms: float = 0.0
    tts_audio_bytes: int = 0
    tts_audio_duration_s: float = 0.0


@dataclass
class BenchmarkReport:
    rounds: int = 0
    samples: list[LatencyResult] = field(default_factory=list)

    def add(self, result: LatencyResult) -> None:
        self.samples.append(result)
        self.rounds = len(self.samples)

    def avg(self, attr: str) -> float:
        values = [getattr(s, attr) for s in self.samples if getattr(s, attr) > 0]
        return statistics.mean(values) if values else 0.0

    def print_summary(self) -> None:
        print()
        print("=" * 56)
        print("JoyInside 延迟测试报告")
        print("=" * 56)
        print(f"测试轮次: {self.rounds}")
        print()

        rows = [
            ("Token 获取（冷）", "token_cold_ms", "ms"),
            ("Token 获取（缓存）", "token_cached_ms", "ms"),
            ("WebSocket 连接", "ws_connect_ms", "ms"),
            ("PCM 音频配置", "pcm_config_ms", "ms"),
            ("ASR 音频上传", "asr_upload_ms", "ms"),
            ("ASR 识别（上传完成后）", "asr_recognition_ms", "ms"),
            ("ASR 总延迟", "asr_total_ms", "ms"),
            ("TTS 首包延迟 (TTFB)", "tts_first_audio_ms", "ms"),
            ("TTS 完成延迟", "tts_complete_ms", "ms"),
        ]

        print(f"{'指标':<28} {'平均':>10} {'最小':>10} {'最大':>10}")
        print("-" * 56)
        for label, attr, unit in rows:
            values = [getattr(s, attr) for s in self.samples if getattr(s, attr) > 0]
            if not values:
                continue
            avg_v = statistics.mean(values)
            print(
                f"{label:<24} {avg_v:>8.0f}{unit} "
                f"{min(values):>8.0f}{unit} {max(values):>8.0f}{unit}"
            )

        tts_bytes = [s.tts_audio_bytes for s in self.samples if s.tts_audio_bytes > 0]
        tts_dur = [s.tts_audio_duration_s for s in self.samples if s.tts_audio_duration_s > 0]
        if tts_bytes:
            print()
            print(
                f"TTS 音频大小: 平均 {statistics.mean(tts_bytes):.0f} 字节, "
                f"时长约 {statistics.mean(tts_dur):.2f}s"
            )

        asr_texts = [s.asr_text for s in self.samples if s.asr_text]
        if asr_texts:
            print(f"ASR 识别样例: {asr_texts[-1]!r}")

        # 模拟一轮对话的端到端延迟（不含录音和本地播放）
        e2e = [
            s.asr_total_ms + s.tts_first_audio_ms
            for s in self.samples
            if s.asr_total_ms > 0 and s.tts_first_audio_ms > 0
        ]
        if e2e:
            print()
            print(
                f"估算对话延迟（ASR 完成 → TTS 首包）: "
                f"平均 {statistics.mean(e2e):.0f}ms"
            )
            full = [
                s.asr_total_ms + s.tts_complete_ms
                for s in self.samples
                if s.asr_total_ms > 0 and s.tts_complete_ms > 0
            ]
            print(
                f"估算对话延迟（ASR 完成 → TTS 全部收完）: "
                f"平均 {statistics.mean(full):.0f}ms"
            )

        print("=" * 56)
        print()
        print("说明:")
        print("  - ASR 上传耗时含按实时节奏发送音频（与线上一致）")
        print("  - ASR 识别延迟 = 上传完成到收到 IS_FINAL 的时间")
        print("  - TTS 首包延迟越低，用户越快听到声音")
        print()


def resolve_pcm_path(path: str | None) -> Path:
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"PCM 文件不存在: {p}")
        return p

    for candidate in (ROOT / "output_tts.pcm", ROOT / "test_input.pcm"):
        if candidate.exists():
            return candidate

    # 生成 2 秒静音 PCM 作为兜底（ASR 可能无结果，但可测上传耗时）
    silent = b"\x00\x00" * (SAMPLE_RATE * 2)
    fallback = ROOT / "_latency_test_silent.pcm"
    fallback.write_bytes(silent)
    print(f"未找到测试 PCM，已生成 2s 静音文件: {fallback.name}")
    return fallback


def measure_asr(speech: JoyInsideSpeech, pcm_data: bytes) -> tuple[float, float, float, str]:
    """返回 (upload_ms, recognition_ms, total_ms, text)。"""
    chunks = chunk_pcm(pcm_data)
    result: dict[str, str] = {}
    done = threading.Event()

    def on_asr(text: str) -> None:
        result["text"] = text
        done.set()

    prev = speech.on_asr_final
    speech.on_asr_final = on_asr

    try:
        t0 = time.perf_counter()
        speech.recognize_pcm(chunks)
        upload_ms = (time.perf_counter() - t0) * 1000

        if not done.wait(30.0):
            raise TimeoutError("ASR 超时")

        total_ms = (time.perf_counter() - t0) * 1000
        recognition_ms = max(0.0, total_ms - upload_ms)
        return upload_ms, recognition_ms, total_ms, result.get("text", "")
    finally:
        speech.on_asr_final = prev


def measure_tts(speech: JoyInsideSpeech, text: str) -> tuple[float, float, int, float]:
    """返回 (first_audio_ms, complete_ms, bytes, duration_s)。"""
    chunks: list[bytes] = []
    first_audio_at = 0.0
    t0 = time.perf_counter()
    done = threading.Event()

    def on_audio(data: bytes, _meta: dict) -> None:
        nonlocal first_audio_at
        if data and first_audio_at == 0.0:
            first_audio_at = time.perf_counter()
        if data:
            chunks.append(data)

    def on_complete() -> None:
        done.set()

    prev_audio = speech.on_tts_audio
    prev_complete = speech.on_tts_complete
    speech.on_tts_audio = on_audio
    speech.on_tts_complete = on_complete

    try:
        speech.speak(text)
        if not done.wait(60.0):
            raise TimeoutError("TTS 超时")
        t1 = time.perf_counter()
        audio = b"".join(chunks)
        first_ms = (first_audio_at - t0) * 1000 if first_audio_at else 0.0
        complete_ms = (t1 - t0) * 1000
        duration_s = len(audio) / (SAMPLE_RATE * 2) if audio else 0.0
        return first_ms, complete_ms, len(audio), duration_s
    finally:
        speech.on_tts_audio = prev_audio
        speech.on_tts_complete = prev_complete


def run_round(
    *,
    pcm_data: bytes,
    tts_text: str,
    skip_asr: bool,
    skip_tts: bool,
    measure_token: bool,
) -> LatencyResult:
    result = LatencyResult()
    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    if measure_token:
        t0 = time.perf_counter()
        auth.get_token(bot_id=cfg.bot_id, force_refresh=True)
        result.token_cold_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        auth.get_token(bot_id=cfg.bot_id)
        result.token_cached_ms = (time.perf_counter() - t0) * 1000

    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
    )

    t0 = time.perf_counter()
    speech.connect()
    result.ws_connect_ms = (time.perf_counter() - t0) * 1000

    try:
        if not skip_tts:
            t0 = time.perf_counter()
            speech.ensure_pcm_output()
            result.pcm_config_ms = (time.perf_counter() - t0) * 1000

        if not skip_asr:
            print("  测试 ASR …", flush=True)
            try:
                up, rec, total, text = measure_asr(speech, pcm_data)
                result.asr_upload_ms = up
                result.asr_recognition_ms = rec
                result.asr_total_ms = total
                result.asr_text = text
                print(
                    f"    上传 {up:.0f}ms | 识别 {rec:.0f}ms | "
                    f"合计 {total:.0f}ms | 文本={text!r}"
                )
            except TimeoutError:
                print("    ASR 超时（测试音频可能过短或不适合识别，可换更长的 PCM）")

        if not skip_tts:
            print(f"  测试 TTS … {tts_text!r}", flush=True)
            first, complete, nbytes, dur = measure_tts(speech, tts_text)
            result.tts_first_audio_ms = first
            result.tts_complete_ms = complete
            result.tts_audio_bytes = nbytes
            result.tts_audio_duration_s = dur
            print(
                f"    首包 {first:.0f}ms | 完成 {complete:.0f}ms | "
                f"音频 {nbytes} 字节 ({dur:.2f}s)"
            )
    finally:
        speech.close()

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 延迟基准测试")
    parser.add_argument("--pcm", help="ASR 测试用 PCM 文件（默认 output_tts.pcm）")
    parser.add_argument("--tts-text", default=TTS_TEXT_DEFAULT, help="TTS 测试文本")
    parser.add_argument("--rounds", type=int, default=1, help="测试轮次（默认 1）")
    parser.add_argument("--skip-asr", action="store_true", help="跳过 ASR 测试")
    parser.add_argument("--skip-tts", action="store_true", help="跳过 TTS 测试")
    parser.add_argument(
        "--skip-token",
        action="store_true",
        help="跳过 Token 测试（后续轮次可加此参数）",
    )
    args = parser.parse_args()

    pcm_path = resolve_pcm_path(args.pcm)
    pcm_data = read_pcm_file(pcm_path)
    min_bytes = int(SAMPLE_RATE * 2 * 1.5)
    if len(pcm_data) < min_bytes:
        repeats = (min_bytes // len(pcm_data)) + 1
        pcm_data = (pcm_data * repeats)[:min_bytes]
        print(f"  已将测试音频延长至 {len(pcm_data) / (SAMPLE_RATE * 2):.2f}s（便于 ASR 测试）")
    audio_duration = len(pcm_data) / (SAMPLE_RATE * 2)

    print("JoyInside 延迟测试")
    print(f"  ASR 测试音频: {pcm_path.name} ({audio_duration:.2f}s)")
    print(f"  TTS 测试文本: {args.tts_text!r}")
    print(f"  轮次: {args.rounds}")
    if audio_duration < 1.0:
        print(f"  提示: ASR 测试音频仅 {audio_duration:.2f}s，建议 >1s，否则可能识别超时")
    print()

    report = BenchmarkReport()
    for i in range(args.rounds):
        if args.rounds > 1:
            print(f"--- 第 {i + 1}/{args.rounds} 轮 ---")
        result = run_round(
            pcm_data=pcm_data,
            tts_text=args.tts_text,
            skip_asr=args.skip_asr,
            skip_tts=args.skip_tts,
            measure_token=not args.skip_token and i == 0,
        )
        report.add(result)
        if args.rounds > 1 and i + 1 < args.rounds:
            time.sleep(1.0)

    report.print_summary()


if __name__ == "__main__":
    main()
