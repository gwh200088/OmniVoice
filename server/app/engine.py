"""模型推理引擎。

封装 OmniVoice 的模型加载、声纹特征提取与语音合成，统一在推理锁的保护下
串行执行（避免并发请求把显存打爆），并把关键步骤的耗时交给 StageTimer 统计。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
import torch

from .config import Settings, get_settings
from .logging_setup import get_logger

logger = get_logger(__name__)

# 精度名称 → torch dtype
DTYPE_MAP = {
    "float16": torch.float16,
    "fp16": torch.float16,
    "half": torch.float16,
    "bfloat16": torch.bfloat16,
    "bf16": torch.bfloat16,
    "float32": torch.float32,
    "fp32": torch.float32,
}


def resolve_device(explicit: str = "") -> str:
    """确定推理设备：优先使用显式配置，其次自动检测。"""
    if explicit:
        return explicit
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_dtype(name: str, device: str) -> torch.dtype:
    """确定计算精度。CPU 上强制使用 float32，避免 half 算子不支持。"""
    key = (name or "float16").lower()
    dtype = DTYPE_MAP.get(key, torch.float16)
    if device.startswith("cpu") and dtype in (torch.float16, torch.bfloat16):
        logger.warning("检测到 CPU 推理，精度自动调整为 float32。")
        return torch.float32
    return dtype


class InferenceEngine:
    """OmniVoice 推理引擎（进程内单例）。"""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.model = None
        self.device = ""
        self.dtype = None
        self.sampling_rate = int(self.settings.target_sample_rate)
        self._lock = threading.Lock()      # 模型加载锁
        self._infer_lock = threading.Lock()  # 推理串行锁
        self._load_time_ms: float = 0.0

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self) -> None:
        """加载模型（线程安全，重复调用只会加载一次）。"""
        with self._lock:
            if self.model is not None:
                return

            from omnivoice import OmniVoice

            if self.settings.hf_endpoint:
                import os

                os.environ.setdefault("HF_ENDPOINT", self.settings.hf_endpoint)

            self.device = resolve_device(self.settings.device)
            self.dtype = resolve_dtype(self.settings.dtype, self.device)

            logger.info(
                "开始加载模型：模型=%s，设备=%s，精度=%s",
                self.settings.model_id,
                self.device,
                self.dtype,
            )

            kwargs = {
                "device_map": self.device,
                "dtype": self.dtype,
                "load_asr": bool(self.settings.load_asr),
                "asr_model_name": self.settings.asr_model or None,
            }
            if self.settings.asr_device:
                kwargs["asr_device"] = self.settings.asr_device
            if self.settings.attn_implementation:
                kwargs["attn_implementation"] = self.settings.attn_implementation

            started = time.perf_counter()
            self.model = OmniVoice.from_pretrained(self.settings.model_id, **kwargs)
            self.model.eval()

            if self.device.startswith("cuda"):
                torch.cuda.synchronize()
            self._load_time_ms = (time.perf_counter() - started) * 1000.0

            self.sampling_rate = int(getattr(self.model, "sampling_rate", 0) or self.sampling_rate)

            logger.info(
                "模型加载完成，耗时 %.1f 毫秒；采样率=%s Hz；设备=%s",
                self._load_time_ms,
                self.sampling_rate,
                self.device,
            )
            if self.device.startswith("cuda"):
                try:
                    name = torch.cuda.get_device_name(0)
                    total = torch.cuda.get_device_properties(0).total_memory / 1024**3
                    logger.info("显卡信息：%s，显存 %.1f GB", name, total)
                except Exception:  # 取显卡信息失败不影响主流程
                    pass

    def ensure_loaded(self) -> None:
        """确保模型已加载（首次请求触发懒加载）。"""
        if self.model is None:
            self.load()

    # ------------------------------------------------------------------
    # 特征提取
    # ------------------------------------------------------------------
    def extract_prompt(
        self,
        audio_path: str,
        ref_text: Optional[str] = None,
        preprocess: bool = True,
        timer=None,
    ):
        """从源音频提取可复用的声纹特征。

        Args:
            audio_path: 已规范化为 wav 的音频路径。
            ref_text: 参考文本；为 None 或空时调用 ASR 自动识别。
            preprocess: 是否做静音裁剪等预处理。
            timer: 阶段计时器，用于记录耗时。

        Returns:
            (VoiceClonePrompt, 参考文本, 文本来源)
        """
        self.ensure_loaded()

        stage = (
            timer.stage("声纹特征提取")
            if timer is not None
            else _null_context()
        )
        started = time.perf_counter()
        with stage:
            with self._infer_lock:
                text = (ref_text or "").strip() or None
                source = "用户填写" if text else "ASR 自动识别"
                prompt = self.model.create_voice_clone_prompt(
                    ref_audio=str(audio_path),
                    ref_text=text,
                    preprocess_prompt=preprocess,
                )
                if text is None:
                    text = getattr(prompt, "ref_text", "") or ""
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.info(
            "声纹特征提取完成，耗时 %.1f 毫秒，参考文本来源=%s，参考文本=%s",
            elapsed,
            source,
            (text or "")[:80],
        )
        return prompt, text or "", source

    # ------------------------------------------------------------------
    # 语音合成
    # ------------------------------------------------------------------
    def synthesize(
        self,
        text: str,
        prompt=None,
        ref_text: Optional[str] = None,
        language: Optional[str] = None,
        instruct: Optional[str] = None,
        duration: Optional[float] = None,
        speed: Optional[float] = None,
        num_step: Optional[int] = None,
        guidance_scale: Optional[float] = None,
        denoise: bool = True,
        preprocess_prompt: bool = True,
        postprocess_output: bool = True,
        timer=None,
    ) -> np.ndarray:
        """执行一次语音合成，返回 1 维浮点波形。

        推理过程在锁内串行执行；`timer` 存在时会记录"语音合成"阶段耗时。
        """
        self.ensure_loaded()

        from omnivoice import OmniVoiceGenerationConfig

        config = OmniVoiceGenerationConfig(
            num_step=int(num_step or self.settings.default_num_step),
            guidance_scale=float(
                guidance_scale
                if guidance_scale is not None
                else self.settings.default_guidance_scale
            ),
            denoise=bool(denoise),
            preprocess_prompt=bool(preprocess_prompt),
            postprocess_output=bool(postprocess_output),
        )

        kwargs = {
            "text": text,
            "generation_config": config,
        }
        if language:
            kwargs["language"] = language
        if prompt is not None:
            # 传入已提取好的特征，跳过重复的音频编码与 ASR
            kwargs["voice_clone_prompt"] = prompt
        elif ref_text:
            kwargs["ref_text"] = ref_text
        if instruct:
            kwargs["instruct"] = instruct
        if duration and float(duration) > 0:
            kwargs["duration"] = float(duration)
        if speed and float(speed) != 1.0:
            kwargs["speed"] = float(speed)

        stage = timer.stage("语音合成") if timer is not None else _null_context()
        started = time.perf_counter()
        with stage:
            with self._infer_lock:
                audios = self.model.generate(**kwargs)
        elapsed = (time.perf_counter() - started) * 1000.0
        waveform = np.asarray(audios[0], dtype=np.float32)
        logger.info(
            "语音合成完成，耗时 %.1f 毫秒，文本长度=%d 字，音频时长=%.2f 秒",
            elapsed,
            len(text),
            len(waveform) / float(self.sampling_rate or 24000),
        )
        return waveform

    # ------------------------------------------------------------------
    # 运行信息
    # ------------------------------------------------------------------
    def info(self) -> dict:
        """返回引擎运行状态。"""
        device_name = ""
        if self.device.startswith("cuda") and torch.cuda.is_available():
            try:
                device_name = torch.cuda.get_device_name(0)
            except Exception:
                device_name = ""
        return {
            "模型": self.settings.model_id,
            "模型已加载": self.is_loaded,
            "推理设备": self.device or "未加载",
            "显卡名称": device_name,
            "计算精度": str(self.dtype) if self.dtype else "未加载",
            "采样率": self.sampling_rate,
            "模型加载耗时毫秒": round(self._load_time_ms, 1),
        }


class _NullContext:
    """无操作上下文，用于未传入计时器时的占位。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _null_context():
    return _NullContext()


_engine: Optional[InferenceEngine] = None
_engine_lock = threading.Lock()


def get_engine(settings: Optional[Settings] = None) -> InferenceEngine:
    """获取全局唯一的推理引擎实例。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = InferenceEngine(settings or get_settings())
    return _engine
