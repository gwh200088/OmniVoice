"""源音频与声纹特征管理（本地持久化）。

每个音色在磁盘上对应一个目录，结构如下：::

    data/voices/<音色ID>/
        source.wav   规范化后的源音频（单声道、统一采样率）
        prompt.pt    提取好的声纹特征（VoiceClonePrompt）
        meta.json    音色元数据

特征以文件形式落盘，并通过宿主机目录挂载实现持久化；容器重启后
无需重新提取，直接使用 prompt.pt 即可合成。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from omnivoice import VoiceClonePrompt

from .audio_utils import guess_extension, normalize_to_wav, probe_audio_info
from .logging_setup import get_logger

logger = get_logger(__name__)

# 音色目录内的固定文件名
SOURCE_AUDIO_NAME = "source.wav"
FEATURE_NAME = "prompt.pt"
META_NAME = "meta.json"


def _now() -> str:
    """返回当前时间的中文可读字符串。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _file_md5(path: Path | str, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 MD5，用于识别重复上传的同一段音频。"""
    digest = hashlib.md5()
    with open(str(path), "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再替换，避免写入中断产生损坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(handle.name, str(path))
    finally:
        if os.path.exists(handle.name):
            try:
                os.unlink(handle.name)
            except OSError:
                pass


class VoiceStore:
    """音色仓库：负责源音频、特征文件与元数据的读写。"""

    def __init__(self, root: Path | str, cache_size: int = 64) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_size = max(0, int(cache_size))
        # 特征内存缓存（LRU），避免同一音色反复读盘
        self._cache: "OrderedDict[str, VoiceClonePrompt]" = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 路径工具
    # ------------------------------------------------------------------
    def voice_dir(self, voice_id: str) -> Path:
        return self.root / voice_id

    def audio_path(self, voice_id: str) -> Path:
        return self.voice_dir(voice_id) / SOURCE_AUDIO_NAME

    def feature_path(self, voice_id: str) -> Path:
        return self.voice_dir(voice_id) / FEATURE_NAME

    def meta_path(self, voice_id: str) -> Path:
        return self.voice_dir(voice_id) / META_NAME

    def new_voice_id(self) -> str:
        """生成新的音色 ID（时间戳 + 随机串，保证可读且不重复）。"""
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"voice_{stamp}_{os.urandom(3).hex()}"

    # ------------------------------------------------------------------
    # 创建与写入
    # ------------------------------------------------------------------
    def create(
        self,
        src_path: Path | str,
        original_filename: str,
        name: str = "",
        ref_text: str = "",
        sample_rate: int = 24000,
        max_duration: float | None = None,
    ) -> dict:
        """登记一段源音频：转码、落盘并写入元数据。

        Args:
            src_path: 上传的临时音频文件。
            original_filename: 用户原始文件名（用于记录格式）。
            name: 音色名称，留空时自动使用文件名。
            ref_text: 参考音频对应的文本，留空则在提取特征时用 ASR 识别。
            sample_rate: 规范化音频的目标采样率。
            max_duration: 参考音频最大保留时长（秒）。

        Returns:
            音色元数据字典。
        """
        src_path = Path(src_path)
        voice_id = self.new_voice_id()
        directory = self.voice_dir(voice_id)
        directory.mkdir(parents=True, exist_ok=True)

        raw_md5 = _file_md5(src_path)
        raw_format = guess_extension(original_filename)
        raw_info = probe_audio_info(src_path)

        # 统一转码为模型友好的 wav，后续提取与复用都不再依赖 ffmpeg
        normalize_to_wav(
            src_path,
            self.audio_path(voice_id),
            sample_rate=sample_rate,
            max_duration=max_duration,
        )
        norm_info = probe_audio_info(self.audio_path(voice_id))

        meta = {
            "音色ID": voice_id,
            "音色名称": (name or "").strip() or Path(original_filename or "未命名音色").stem,
            "创建时间": _now(),
            "更新时间": _now(),
            "原始文件名": original_filename or "",
            "原始格式": raw_format,
            "原始文件大小字节": src_path.stat().st_size if src_path.exists() else 0,
            "文件MD5": raw_md5,
            "原始音频时长秒": round(float(raw_info.get("时长秒", 0.0) or 0.0), 3),
            "音频时长秒": round(float(norm_info.get("时长秒", 0.0) or 0.0), 3),
            "采样率": int(norm_info.get("采样率", sample_rate) or sample_rate),
            "声道数": int(norm_info.get("声道数", 1) or 1),
            "参考文本": (ref_text or "").strip(),
            "参考文本来源": "用户填写" if (ref_text or "").strip() else "待识别",
            "是否已提取特征": False,
            "特征提取耗时毫秒": 0.0,
            "特征提取时间": "",
            "特征文件大小字节": 0,
            "特征文件路径": str(self.feature_path(voice_id)),
            "源音频文件路径": str(self.audio_path(voice_id)),
        }

        with self._lock:
            _atomic_write_json(self.meta_path(voice_id), meta)

        logger.info(
            "已登记源音频：音色ID=%s，名称=%s，格式=%s，时长=%.2f 秒",
            voice_id,
            meta["音色名称"],
            raw_format,
            meta["音频时长秒"],
        )
        return meta

    def update_meta(self, voice_id: str, **fields) -> dict:
        """局部更新音色元数据，返回更新后的完整元数据。"""
        with self._lock:
            meta = self._read_meta(voice_id)
            if meta is None:
                raise KeyError(voice_id)
            for key, value in fields.items():
                if value is None:
                    continue
                meta[key] = value
            meta["更新时间"] = _now()
            _atomic_write_json(self.meta_path(voice_id), meta)
        return meta

    def mark_extracted(
        self,
        voice_id: str,
        elapsed_ms: float,
        ref_text: str = "",
        ref_text_source: str = "",
    ) -> dict:
        """特征提取完成后写回元数据。"""
        fields = {
            "是否已提取特征": True,
            "特征提取耗时毫秒": round(float(elapsed_ms), 1),
            "特征提取时间": _now(),
            "特征文件大小字节": (
                self.feature_path(voice_id).stat().st_size
                if self.feature_path(voice_id).exists()
                else 0
            ),
        }
        if ref_text:
            fields["参考文本"] = ref_text
        if ref_text_source:
            fields["参考文本来源"] = ref_text_source
        return self.update_meta(voice_id, **fields)

    def save_feature(self, voice_id: str, prompt: VoiceClonePrompt) -> Path:
        """把声纹特征保存到磁盘，并同步刷新内存缓存。"""
        path = self.feature_path(voice_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        prompt.save(str(path))
        with self._lock:
            self._cache[voice_id] = prompt
            self._cache.move_to_end(voice_id)
            self._trim_cache()
        return path

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _read_meta(self, voice_id: str) -> Optional[dict]:
        path = self.meta_path(voice_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取音色元数据失败（音色ID=%s）：%s", voice_id, exc)
            return None

    def get(self, voice_id: str) -> Optional[dict]:
        """按 ID 获取音色元数据，不存在时返回 None。"""
        with self._lock:
            return self._read_meta(voice_id)

    def require(self, voice_id: str) -> dict:
        """按 ID 获取音色元数据，不存在时抛出异常。"""
        meta = self.get(voice_id)
        if meta is None:
            raise KeyError(f"音色不存在：{voice_id}")
        return meta

    def list(self) -> List[dict]:
        """列出所有音色，按创建时间倒序。"""
        items: List[dict] = []
        if not self.root.exists():
            return items
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta = self._read_meta(child.name)
            if meta is not None:
                items.append(meta)
        items.sort(key=lambda item: item.get("创建时间", ""), reverse=True)
        return items

    def has_feature(self, voice_id: str) -> bool:
        """判断指定音色的特征文件是否已存在。"""
        return self.feature_path(voice_id).exists()

    def load_feature(self, voice_id: str) -> Optional[VoiceClonePrompt]:
        """加载特征；优先命中内存缓存，miss 时从磁盘读取。

        Returns:
            已提取特征时返回 VoiceClonePrompt，否则返回 None。
        """
        with self._lock:
            cached = self._cache.get(voice_id)
            if cached is not None:
                self._cache.move_to_end(voice_id)
                logger.info("特征缓存命中（内存），音色ID=%s", voice_id)
                return cached

        path = self.feature_path(voice_id)
        if not path.exists():
            return None

        started = time.perf_counter()
        prompt = VoiceClonePrompt.load(str(path), map_location="cpu")
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.info(
            "特征缓存命中（磁盘文件），音色ID=%s，加载耗时 %.1f 毫秒",
            voice_id,
            elapsed,
        )

        with self._lock:
            self._cache[voice_id] = prompt
            self._cache.move_to_end(voice_id)
            self._trim_cache()
        return prompt

    def _trim_cache(self) -> None:
        """按 LRU 淘汰超出容量的内存缓存（调用方需已持有锁）。"""
        while self.cache_size >= 0 and len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def drop_cache(self, voice_id: str) -> None:
        """丢弃指定音色的内存缓存。"""
        with self._lock:
            self._cache.pop(voice_id, None)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def delete(self, voice_id: str) -> bool:
        """删除音色及其源音频、特征文件；不存在时返回 False。"""
        directory = self.voice_dir(voice_id)
        if not directory.exists():
            return False
        with self._lock:
            self._cache.pop(voice_id, None)
            shutil.rmtree(directory, ignore_errors=True)
        logger.info("已删除音色：音色ID=%s", voice_id)
        return True

    def stats(self) -> Dict[str, object]:
        """返回仓库统计信息，用于健康检查与页面展示。"""
        items = self.list()
        extracted = sum(1 for item in items if item.get("是否已提取特征"))
        total_bytes = 0
        for item in items:
            try:
                total_bytes += int(item.get("特征文件大小字节", 0) or 0)
            except (TypeError, ValueError):
                pass
        return {
            "音色总数": len(items),
            "已提取特征数": extracted,
            "内存缓存条数": len(self._cache),
            "特征占用字节": total_bytes,
            "存储根目录": str(self.root),
        }
