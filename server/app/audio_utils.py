"""音频处理工具。

负责把用户上传的各种格式（wav / mp3 / m4a / flac / ogg ...）统一转码成
模型可直接使用的单声道 wav，并获取音频时长等基础信息。

优先使用 ffmpeg（格式支持最全）；当环境中没有 ffmpeg 时，自动回退到
soundfile / librosa，保证 wav、flac、ogg 等常见格式仍可正常工作。
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from .logging_setup import get_logger

logger = get_logger(__name__)


def ffmpeg_available() -> bool:
    """判断系统是否已安装 ffmpeg。"""
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    """判断系统是否已安装 ffprobe。"""
    return shutil.which("ffprobe") is not None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """执行命令并返回结果（不抛异常，交由调用方处理）。"""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def probe_duration(path: Path | str) -> float:
    """获取音频时长（秒）。无法探测时返回 0.0。"""
    path = str(path)
    if ffprobe_available():
        result = _run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ]
        )
        try:
            return float(result.stdout.decode().strip())
        except (ValueError, AttributeError):
            pass

    # 回退：用 soundfile 读取
    try:
        info = sf.info(path)
        if info.samplerate and info.frames:
            return float(info.frames) / float(info.samplerate)
    except Exception:
        pass
    return 0.0


def probe_audio_info(path: Path | str) -> dict:
    """读取音频基础信息：时长、采样率、声道数、编码格式。"""
    path = str(path)
    info: dict = {
        "时长秒": 0.0,
        "采样率": 0,
        "声道数": 0,
        "音频编码": "",
        "容器格式": "",
    }

    if ffprobe_available():
        result = _run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries",
                "stream=codec_name,channels,sample_rate:format=duration,format_name",
                "-of", "default=noprint_wrappers=1",
                path,
            ]
        )
        text = result.stdout.decode(errors="ignore")
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key == "duration" and value not in ("", "N/A"):
                try:
                    info["时长秒"] = round(float(value), 3)
                except ValueError:
                    pass
            elif key == "sample_rate" and value not in ("", "N/A"):
                try:
                    info["采样率"] = int(value)
                except ValueError:
                    pass
            elif key == "channels" and value not in ("", "N/A"):
                try:
                    info["声道数"] = int(value)
                except ValueError:
                    pass
            elif key == "codec_name":
                info["音频编码"] = value
            elif key == "format_name":
                info["容器格式"] = value
        if info["时长秒"] > 0:
            return info

    try:
        meta = sf.info(path)
        info["时长秒"] = round(float(meta.frames) / float(meta.samplerate), 3)
        info["采样率"] = int(meta.samplerate)
        info["声道数"] = int(meta.channels)
        info["容器格式"] = str(meta.format)
    except Exception:
        pass
    return info


def normalize_to_wav(
    src_path: Path | str,
    dst_path: Path | str,
    sample_rate: int = 24000,
    max_duration: float | None = None,
) -> dict:
    """把任意格式音频转码为单声道、指定采样率的 16 位 wav。

    Args:
        src_path: 源音频路径。
        dst_path: 目标 wav 路径。
        sample_rate: 目标采样率。
        max_duration: 超过该时长（秒）时只保留前 max_duration 秒，
            避免超长参考音频拖慢推理、占用过多显存。

    Returns:
        转码后音频的基础信息字典。

    Raises:
        RuntimeError: 转码失败时抛出。
    """
    src_path, dst_path = str(src_path), str(dst_path)
    Path(dst_path).parent.mkdir(parents=True, exist_ok=True)

    if ffmpeg_available():
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path,
            "-vn",                    # 丢弃封面等视频流
            "-ac", "1",               # 转单声道
            "-ar", str(sample_rate),  # 重采样
            "-sample_fmt", "s16",     # 16 位 PCM
            "-f", "wav",
        ]
        if max_duration and max_duration > 0:
            cmd += ["-t", f"{max_duration:.3f}"]
        cmd += [dst_path]

        result = _run(cmd)
        if result.returncode != 0 or not Path(dst_path).exists():
            message = result.stderr.decode(errors="ignore").strip()
            logger.warning(
                "ffmpeg 转码失败（错误码 %s）：%s，尝试回退到 soundfile / librosa。",
                result.returncode,
                message[:300],
            )
        else:
            return probe_audio_info(dst_path)

    # ---- 回退方案：soundfile 直读，读不了再用 librosa 解码压缩格式 ----
    try:
        data, sr = sf.read(src_path, dtype="float32", always_2d=True)
        data = data.T  # (T, C) → (C, T)
    except Exception:
        import librosa

        data, sr = librosa.load(src_path, sr=None, mono=False)
        if data.ndim == 1:
            data = data[np.newaxis, :]
        data = data.astype(np.float32)

    if data.shape[0] > 1:
        data = np.mean(data, axis=0, keepdims=True)

    if max_duration and max_duration > 0:
        limit = int(max_duration * sr)
        if data.shape[-1] > limit:
            data = data[:, :limit]

    if sr != sample_rate:
        import torch
        import torchaudio

        data = torchaudio.functional.resample(
            torch.from_numpy(data), orig_freq=sr, new_freq=sample_rate
        ).numpy()

    sf.write(dst_path, data.T, sample_rate, subtype="PCM_16")
    return probe_audio_info(dst_path)


def wav_bytes(waveform: np.ndarray, sample_rate: int) -> bytes:
    """把浮点波形（值域约 [-1, 1]）编码成 wav 文件的二进制内容。"""
    buffer = io.BytesIO()
    audio = np.asarray(waveform, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    sf.write(buffer, audio, sample_rate, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def save_wav(waveform: np.ndarray, sample_rate: int, path: Path | str) -> str:
    """把波形保存为 wav 文件，返回文件路径。"""
    path = str(path)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    audio = np.clip(np.asarray(waveform, dtype=np.float32), -1.0, 1.0)
    sf.write(path, audio, sample_rate, subtype="PCM_16")
    return path


def guess_extension(filename: str, default: str = "audio") -> str:
    """从文件名中提取小写扩展名（不含点号）。"""
    suffix = Path(filename or "").suffix
    return suffix.lstrip(".").lower() or default
