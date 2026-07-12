"""PCM 音频工具。"""

from __future__ import annotations

from pathlib import Path

from config import BYTES_PER_FRAME, BYTES_PER_MS


def read_pcm_file(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def chunk_pcm(
    pcm_data: bytes,
    frame_bytes: int = BYTES_PER_FRAME,
) -> list[bytes]:
    """将 PCM 数据切成固定大小帧。"""
    chunks: list[bytes] = []
    for offset in range(0, len(pcm_data), frame_bytes):
        chunks.append(pcm_data[offset : offset + frame_bytes])
    return chunks


def frame_duration_seconds(frame_bytes: int) -> float:
    """根据帧字节数计算应等待的时长（秒）。"""
    return frame_bytes / BYTES_PER_MS / 1000.0
