"""
JoyInside 延迟基准测试（支持批处理 / 流式对比）。

用法:
  python examples/latency_benchmark.py --compare --rounds 2
  python examples/latency_benchmark.py --mode streaming --skip-asr
  python examples/latency_benchmark.py --mode batch --pcm test_input.pcm
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

from config import BYTES_PER_FRAME, SAMPLE_RATE, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, frame_duration_seconds, read_pcm_file
from joyinside.speech import JoyInsideSpeech

TTS_TEXT_DEFAULT = "你好，我是语音助手，有什么可以帮你的？"


@dataclass
class LatencyResult:
    mode: str = "batch"
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

    @property
    def tts_perceived_ms(self) -> float:
        """用户感知延迟：流式=首包，批处理=全部收完。"""
        if self.mode == "streaming":
            return self.tts_first_audio_ms
        return self.tts_complete_ms

    @property
    def dialog_perceived_ms(self) -> float:
        """估算对话感知延迟（ASR 完成 → 开始听到回复）。"""
        if self.asr_total_ms <= 0:
            return 0.0
        return self.asr_total_ms + self.tts_perceived_ms


@dataclass
class BenchmarkReport:
    label: str = ""
    samples: list[LatencyResult] = field(default_factory=list)

    def add(self, result: LatencyResult) -> None:
        self.samples.append(result)

    def _values(self, attr: str) -> list[float]:
        return [getattr(s, attr) for s in self.samples if getattr(s, attr) > 0]

    def print_summary(self) -> None:
        if not self.samples:
            return
        title = self.label or self.samples[0].mode
        print()
        print("=" * 60)
        print(f"延迟报告 — {title} 模式")
        print("=" * 60)
        print(f"轮次: {len(self.samples)}")
        print()

        rows = [
            ("Token（冷）", "token_cold_ms"),
            ("Token（缓存）", "token_cached_ms"),
            ("WebSocket 连接", "ws_connect_ms"),
            ("PCM 配置", "pcm_config_ms"),
            ("ASR 上传", "asr_upload_ms"),
            ("ASR 识别（上传后）", "asr_recognition_ms"),
            ("ASR 总延迟", "asr_total_ms"),
            ("TTS 首包 (TTFB)", "tts_first_audio_ms"),
            ("TTS 全部收完", "tts_complete_ms"),
            ("TTS 感知延迟", "tts_perceived_ms"),
            ("对话感知延迟", "dialog_perceived_ms"),
        ]

        print(f"{'指标':<22} {'平均':>8} {'最小':>8} {'最大':>8}")
        print("-" * 60)
        for label, attr in rows:
            values = self._values(attr)
            if not values:
                continue
            avg_v = statistics.mean(values)
            print(
                f"{label:<22} {avg_v:>6.0f}ms "
                f"{min(values):>6.0f}ms {max(values):>6.0f}ms"
            )

        tts_bytes = [s.tts_audio_bytes for s in self.samples if s.tts_audio_bytes > 0]
        if tts_bytes:
            print()
            print(f"TTS 音频: 平均 {statistics.mean(tts_bytes):.0f} 字节")

        texts = [s.asr_text for s in self.samples if s.asr_text]
        if texts:
            print(f"ASR 样例: {texts[-1]!r}")

        print("=" * 60)


def print_compare(batch: BenchmarkReport, streaming: BenchmarkReport) -> None:
    print()
    print("#" * 60)
    print("批处理 vs 流式 对比")
    print("#" * 60)

    metrics = [
        ("ASR 总延迟", "asr_total_ms"),
        ("TTS 首包", "tts_first_audio_ms"),
        ("TTS 感知延迟", "tts_perceived_ms"),
        ("对话感知延迟", "dialog_perceived_ms"),
    ]

    print(f"{'指标':<18} {'批处理':>10} {'流式':>10} {'节省':>10}")
    print("-" * 60)
    for label, attr in metrics:
        b_vals = batch._values(attr)
        s_vals = streaming._values(attr)
        if not b_vals and not s_vals:
            continue
        b_avg = statistics.mean(b_vals) if b_vals else 0
        s_avg = statistics.mean(s_vals) if s_vals else 0
        saved = b_avg - s_avg
        saved_str = f"{saved:+.0f}ms" if saved else "—"
        print(f"{label:<18} {b_avg:>8.0f}ms {s_avg:>8.0f}ms {saved_str:>10}")

    print()
    print("说明:")
    print("  批处理 TTS 感知延迟 = 全部收完才开始播放")
    print("  流式 TTS 感知延迟 = 首包到达即开始播放")
    print("  流式 ASR 上传可与说话重叠，总延迟通常更低")
    print("#" * 60)


def resolve_pcm_path(path: str | None) -> Path:
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"PCM 文件不存在: {p}")
        return p

    for candidate in (ROOT / "test_input.pcm", ROOT / "output_tts.pcm"):
        if candidate.exists():
            return candidate

    silent = b"\x00\x00" * (SAMPLE_RATE * 2)
    fallback = ROOT / "_latency_test_silent.pcm"
    fallback.write_bytes(silent)
    print(f"未找到测试 PCM，已生成 2s 静音: {fallback.name}")
    return fallback


def _wait_asr(speech: JoyInsideSpeech, t0: float) -> tuple[float, float, float, str]:
    result: dict[str, str] = {}
    done = threading.Event()

    def on_asr(text: str) -> None:
        result["text"] = text
        done.set()

    prev = speech.on_asr_final
    speech.on_asr_final = on_asr
    try:
        if not done.wait(30.0):
            raise TimeoutError("ASR 超时")
        total_ms = (time.perf_counter() - t0) * 1000
        return 0.0, 0.0, total_ms, result.get("text", "")
    finally:
        speech.on_asr_final = prev


def measure_asr_batch(speech: JoyInsideSpeech, pcm_data: bytes) -> tuple[float, float, float, str]:
    """批处理 ASR：录完再按实时节奏上传。"""
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


def measure_asr_streaming(speech: JoyInsideSpeech, pcm_data: bytes) -> tuple[float, float, float, str]:
    """流式 ASR：边录边传（基准测试中连续快速发送各帧）。"""
    chunks = chunk_pcm(pcm_data)
    result: dict[str, str] = {}
    done = threading.Event()
    first_sent_at = 0.0
    last_sent_at = 0.0

    def on_asr(text: str) -> None:
        result["text"] = text
        done.set()

    prev = speech.on_asr_final
    speech.on_asr_final = on_asr
    speech.begin_asr()

    try:
        t0 = time.perf_counter()
        for i, chunk in enumerate(chunks):
            if not chunk:
                continue
            if first_sent_at == 0.0:
                first_sent_at = time.perf_counter()
            is_last = i == len(chunks) - 1
            speech.stream_asr_chunk(chunk, is_last=is_last, pace=True)
            last_sent_at = time.perf_counter()

        speech.finish_asr()
        upload_ms = (last_sent_at - first_sent_at) * 1000 if first_sent_at else 0.0

        if not done.wait(30.0):
            raise TimeoutError("ASR 超时")

        total_ms = (time.perf_counter() - t0) * 1000
        # 流式优势：识别可在末帧上传期间并行，用「末帧上传完成→结果」衡量
        recognition_ms = max(0.0, total_ms - upload_ms)
        return upload_ms, recognition_ms, total_ms, result.get("text", "")
    finally:
        speech.on_asr_final = prev


def measure_tts(speech: JoyInsideSpeech, text: str) -> tuple[float, float, int, float]:
    """TTS 下行指标（首包 + 完成），流式/批处理网络侧相同。"""
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
    prev_complete = speech.on_round_complete
    speech.on_tts_audio = on_audio
    speech.on_round_complete = on_complete

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
        speech.on_round_complete = prev_complete


def run_round(
    *,
    mode: str,
    pcm_data: bytes,
    tts_text: str,
    skip_asr: bool,
    skip_tts: bool,
    measure_token: bool,
) -> LatencyResult:
    result = LatencyResult(mode=mode)
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
        manual_mode=True,
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
            print(f"  [{mode}] ASR …", flush=True)
            measure_fn = measure_asr_streaming if mode == "streaming" else measure_asr_batch
            try:
                up, rec, total, text = measure_fn(speech, pcm_data)
                result.asr_upload_ms = up
                result.asr_recognition_ms = rec
                result.asr_total_ms = total
                result.asr_text = text
                print(
                    f"    上传 {up:.0f}ms | 识别 {rec:.0f}ms | "
                    f"合计 {total:.0f}ms | {text!r}",
                    flush=True,
                )
            except TimeoutError:
                print("    ASR 超时（请换真人语音 PCM，如 test_input.pcm）", flush=True)

        if not skip_tts:
            print(f"  [{mode}] TTS …", flush=True)
            first, complete, nbytes, dur = measure_tts(speech, tts_text)
            result.tts_first_audio_ms = first
            result.tts_complete_ms = complete
            result.tts_audio_bytes = nbytes
            result.tts_audio_duration_s = dur
            perceived = result.tts_perceived_ms
            print(
                f"    首包 {first:.0f}ms | 完成 {complete:.0f}ms | "
                f"感知 {perceived:.0f}ms | {nbytes}B",
                flush=True,
            )
    finally:
        speech.close()

    return result


def run_benchmark(
    mode: str,
    *,
    pcm_data: bytes,
    tts_text: str,
    rounds: int,
    skip_asr: bool,
    skip_tts: bool,
) -> BenchmarkReport:
    report = BenchmarkReport(label=mode)
    for i in range(rounds):
        if rounds > 1:
            print(f"--- {mode} 第 {i + 1}/{rounds} 轮 ---", flush=True)
        result = run_round(
            mode=mode,
            pcm_data=pcm_data,
            tts_text=tts_text,
            skip_asr=skip_asr,
            skip_tts=skip_tts,
            measure_token=i == 0,
        )
        report.add(result)
        if i + 1 < rounds:
            time.sleep(1.0)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 延迟基准测试")
    parser.add_argument("--pcm", help="ASR 测试 PCM（推荐 test_input.pcm）")
    parser.add_argument("--tts-text", default=TTS_TEXT_DEFAULT)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--mode", choices=["batch", "streaming"], default="streaming")
    parser.add_argument("--compare", action="store_true", help="对比批处理与流式")
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-tts", action="store_true")
    args = parser.parse_args()

    pcm_path = resolve_pcm_path(args.pcm)
    pcm_data = read_pcm_file(pcm_path)
    min_bytes = int(SAMPLE_RATE * 2 * 1.5)
    if len(pcm_data) < min_bytes and not args.skip_asr:
        repeats = (min_bytes // max(len(pcm_data), 1)) + 1
        pcm_data = (pcm_data * repeats)[:min_bytes]
        print(f"  测试音频延长至 {len(pcm_data) / (SAMPLE_RATE * 2):.2f}s")

    print("JoyInside 延迟测试")
    print(f"  PCM: {pcm_path.name} ({len(pcm_data) / (SAMPLE_RATE * 2):.2f}s)")
    print(f"  TTS: {args.tts_text!r}")
    print(f"  轮次: {args.rounds}")
    print()

    if args.compare:
        batch_report = run_benchmark(
            "batch",
            pcm_data=pcm_data,
            tts_text=args.tts_text,
            rounds=args.rounds,
            skip_asr=args.skip_asr,
            skip_tts=args.skip_tts,
        )
        streaming_report = run_benchmark(
            "streaming",
            pcm_data=pcm_data,
            tts_text=args.tts_text,
            rounds=args.rounds,
            skip_asr=args.skip_asr,
            skip_tts=args.skip_tts,
        )
        batch_report.print_summary()
        streaming_report.print_summary()
        print_compare(batch_report, streaming_report)
    else:
        report = run_benchmark(
            args.mode,
            pcm_data=pcm_data,
            tts_text=args.tts_text,
            rounds=args.rounds,
            skip_asr=args.skip_asr,
            skip_tts=args.skip_tts,
        )
        report.print_summary()


if __name__ == "__main__":
    main()
