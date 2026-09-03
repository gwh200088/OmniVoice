"""业务编排层。

把"上传 → 转码 → 特征提取 → 语音合成 → 编码写出"串成完整流程，
每个阶段单独计时并打印中文日志。HTTP 接口与 Web 页面共用这一层，
保证两条调用路径行为一致。
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Optional

from .audio_utils import (
    guess_extension,
    normalize_to_wav,
    save_wav,
    wav_bytes,
)
from .config import Settings, get_settings
from .engine import InferenceEngine, get_engine
from .logging_setup import get_logger
from .timing import StageTimer, new_request_id
from .voice_store import VoiceStore

logger = get_logger(__name__)


class VoiceCloneService:
    """语音克隆服务：对外提供音色管理与语音合成能力。"""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        engine: Optional[InferenceEngine] = None,
        store: Optional[VoiceStore] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.engine = engine or get_engine(self.settings)
        self.store = store or VoiceStore(
            self.settings.voices_dir, cache_size=self.settings.feature_cache_size
        )

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def validate_upload(self, path: str, filename: str) -> None:
        """校验上传音频的格式与大小，不合规时抛出 ValueError。"""
        ext = guess_extension(filename, default="")
        allowed = [item.lower().lstrip(".") for item in self.settings.allowed_extensions]
        # 扩展名为空时不做拦截，交由后续解码环节兜底（能解码即视为可用）
        if ext and ext not in allowed:
            raise ValueError(
                f"不支持的音频格式：{ext}；当前支持的格式有：{'、'.join(allowed)}"
            )

        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size <= 0:
            raise ValueError("上传的音频文件为空。")
        if size > self.settings.max_upload_bytes:
            raise ValueError(
                f"上传的音频过大（{size / 1024 / 1024:.1f} MB），"
                f"单个文件上限为 {self.settings.max_upload_mb} MB。"
            )

    # ------------------------------------------------------------------
    # 音色管理
    # ------------------------------------------------------------------
    def create_voice(
        self,
        upload_path: str,
        filename: str,
        name: str = "",
        ref_text: str = "",
        extract: bool = True,
        timer: Optional[StageTimer] = None,
    ) -> dict:
        """登记一段源音频，并可立即提取声纹特征保存下来。

        Args:
            upload_path: 上传文件落盘后的临时路径。
            filename: 原始文件名。
            name: 音色名称。
            ref_text: 参考文本，留空则用 ASR 自动识别。
            extract: 是否立即提取特征。
            timer: 外部计时器；为空时内部创建。

        Returns:
            包含"音色信息""阶段耗时""总耗时毫秒"的结果字典。
        """
        own_timer = timer is None
        timer = timer or StageTimer(new_request_id("voice"), task_name="音色创建")

        logger.info(
            "[%s] 收到新建音色请求：文件名=%s，音色名称=%s，立即提取特征=%s",
            timer.request_id,
            filename,
            name or "（未指定）",
            "是" if extract else "否",
        )

        with timer.stage("请求参数校验"):
            self.validate_upload(upload_path, filename)

        with timer.stage("音频解码与转码"):
            meta = self.store.create(
                src_path=upload_path,
                original_filename=filename,
                name=name,
                ref_text=ref_text,
                sample_rate=self.settings.target_sample_rate,
                max_duration=self.settings.max_ref_duration,
            )
        voice_id = meta["音色ID"]

        if extract:
            self._extract_and_save(voice_id, ref_text=ref_text, timer=timer)
            meta = self.store.get(voice_id) or meta

        # 说明：源音频已转码入库，upload_path 由调用方自行清理
        result = {
            "请求编号": timer.request_id,
            "音色信息": meta,
            "阶段耗时": timer.stage_list(),
            "总耗时毫秒": round(timer.total_ms, 1),
        }
        if own_timer:
            timer.summary()
        logger.info(
            "[%s] 新建音色完成：音色ID=%s，是否已提取特征=%s，总耗时 %.1f 毫秒",
            timer.request_id,
            voice_id,
            meta.get("是否已提取特征"),
            timer.total_ms,
        )
        return result

    def reextract_voice(
        self,
        voice_id: str,
        ref_text: Optional[str] = None,
        timer: Optional[StageTimer] = None,
    ) -> dict:
        """重新提取已有音色的声纹特征。"""
        own_timer = timer is None
        timer = timer or StageTimer(new_request_id("reextract"), task_name="特征重新提取")

        with timer.stage("请求参数校验"):
            meta = self.store.require(voice_id)

        text = ref_text if ref_text is not None else meta.get("参考文本", "")
        self._extract_and_save(voice_id, ref_text=text, timer=timer)

        result = {
            "请求编号": timer.request_id,
            "音色信息": self.store.get(voice_id) or meta,
            "阶段耗时": timer.stage_list(),
            "总耗时毫秒": round(timer.total_ms, 1),
        }
        if own_timer:
            timer.summary()
        return result

    def _extract_and_save(
        self,
        voice_id: str,
        ref_text: str = "",
        timer: Optional[StageTimer] = None,
    ) -> None:
        """提取特征并落盘（内部方法）。"""
        meta = self.store.require(voice_id)
        audio_path = meta.get("源音频文件路径") or str(self.store.audio_path(voice_id))

        prompt, text, source = self.engine.extract_prompt(
            audio_path=audio_path,
            ref_text=ref_text or None,
            timer=timer,
        )

        if timer is not None:
            with timer.stage("特征写入磁盘"):
                self.store.save_feature(voice_id, prompt)
            extract_ms = timer.last_stage_ms("声纹特征提取")
        else:
            self.store.save_feature(voice_id, prompt)
            extract_ms = 0.0

        self.store.mark_extracted(
            voice_id,
            elapsed_ms=extract_ms,
            ref_text=text,
            ref_text_source=source,
        )

    # ------------------------------------------------------------------
    # 语音合成
    # ------------------------------------------------------------------
    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        upload_path: Optional[str] = None,
        upload_filename: str = "",
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
        save_as_voice: bool = False,
        voice_name: str = "",
        timer: Optional[StageTimer] = None,
    ) -> dict:
        """执行一次语音合成。

        音色来源二选一：
        1. `voice_id`：使用之前上传并已提取特征的源音频（推荐，最快）；
        2. `upload_path`：本次临时上传的音频，用完即弃（可另存为音色）。

        Returns:
            包含音频二进制、输出路径、阶段耗时等信息的字典。
        """
        own_timer = timer is None
        timer = timer or StageTimer(new_request_id("tts"), task_name="语音合成")

        logger.info(
            "[%s] 收到语音合成请求：文本长度=%d 字，音色来源=%s",
            timer.request_id,
            len(text or ""),
            f"已保存音色 {voice_id}" if voice_id else f"临时上传 {upload_filename or '（无）'}",
        )

        with timer.stage("请求参数校验"):
            text = (text or "").strip()
            if not text:
                raise ValueError("待合成的文本不能为空。")
            if not voice_id and not upload_path:
                raise ValueError("请指定已保存的音色 ID，或临时上传一段参考音频。")
            if voice_id and upload_path:
                raise ValueError("音色 ID 与临时上传音频只能二选一，请勿同时提供。")
            if upload_path:
                self.validate_upload(upload_path, upload_filename)

        prompt = None
        used_voice_id = voice_id
        tmp_wav: Optional[str] = None

        try:
            if voice_id:
                prompt = self._load_or_extract_prompt(voice_id, ref_text, timer)
            elif save_as_voice:
                # 临时上传并留存为音色：登记 + 提取 + 落盘，后续请求可直接复用
                with timer.stage("音频解码与转码"):
                    meta = self.store.create(
                        src_path=upload_path,
                        original_filename=upload_filename,
                        name=voice_name,
                        ref_text=ref_text or "",
                        sample_rate=self.settings.target_sample_rate,
                        max_duration=self.settings.max_ref_duration,
                    )
                used_voice_id = meta["音色ID"]
                prompt, _, _ = self.engine.extract_prompt(
                    audio_path=meta["源音频文件路径"],
                    ref_text=ref_text or None,
                    timer=timer,
                )
                with timer.stage("特征写入磁盘"):
                    self.store.save_feature(used_voice_id, prompt)
                self.store.mark_extracted(
                    used_voice_id,
                    elapsed_ms=timer.last_stage_ms("声纹特征提取"),
                )
            else:
                # 临时上传、用完即弃：转码到临时目录后直接提取特征
                tmp_wav = str(
                    self.settings.tmp_dir / f"tmp_{uuid.uuid4().hex[:12]}.wav"
                )
                with timer.stage("音频解码与转码"):
                    normalize_to_wav(
                        upload_path,
                        tmp_wav,
                        sample_rate=self.settings.target_sample_rate,
                        max_duration=self.settings.max_ref_duration,
                    )
                prompt, _, _ = self.engine.extract_prompt(
                    audio_path=tmp_wav,
                    ref_text=ref_text or None,
                    timer=timer,
                )

            waveform = self.engine.synthesize(
                text=text,
                prompt=prompt,
                language=language or None,
                instruct=instruct or None,
                duration=duration,
                speed=speed,
                num_step=num_step,
                guidance_scale=guidance_scale,
                denoise=denoise,
                preprocess_prompt=preprocess_prompt,
                postprocess_output=postprocess_output,
                timer=timer,
            )

            with timer.stage("音频编码与写出"):
                sample_rate = int(self.engine.sampling_rate or self.settings.target_sample_rate)
                audio_bytes = wav_bytes(waveform, sample_rate)
                output_path = ""
                if self.settings.save_output:
                    filename = f"{time.strftime('%Y%m%d')}_{timer.request_id}.wav"
                    output_path = save_wav(
                        waveform, sample_rate, self.settings.outputs_dir / filename
                    )
        finally:
            # 仅清理本方法自己产生的临时转码文件；上传文件由调用方清理
            self._safe_remove(tmp_wav)

        result = {
            "请求编号": timer.request_id,
            "音频二进制": audio_bytes,
            "音频时长秒": round(len(waveform) / float(sample_rate), 3),
            "采样率": sample_rate,
            "输出文件路径": output_path,
            "使用的音色ID": used_voice_id or "",
            "是否复用已保存特征": bool(voice_id),
            "阶段耗时": timer.stage_list(),
            "总耗时毫秒": round(timer.total_ms, 1),
        }
        if own_timer:
            timer.summary()
        logger.info(
            "[%s] 语音合成完成：输出时长=%.2f 秒，总耗时 %.1f 毫秒",
            timer.request_id,
            result["音频时长秒"],
            result["总耗时毫秒"],
        )
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _load_or_extract_prompt(
        self,
        voice_id: str,
        ref_text: Optional[str],
        timer: StageTimer,
    ):
        """优先复用已保存特征，缺失时现提一次并回写保存。"""
        meta = self.store.require(voice_id)

        with timer.stage("加载已保存特征"):
            prompt = self.store.load_feature(voice_id)
        elapsed = timer.last_stage_ms("加载已保存特征")

        if prompt is not None:
            logger.info(
                "[%s] 已复用音色 %s 的既有特征，跳过重复提取（加载耗时 %.1f 毫秒）",
                timer.request_id,
                voice_id,
                elapsed,
            )
            return prompt

        logger.info(
            "[%s] 音色 %s 尚无特征文件，本次现场提取并保存，后续请求可直接复用。",
            timer.request_id,
            voice_id,
        )
        self._extract_and_save(
            voice_id,
            ref_text=ref_text if ref_text is not None else meta.get("参考文本", ""),
            timer=timer,
        )
        return self.store.load_feature(voice_id)

    @staticmethod
    def _safe_remove(path: Optional[str]) -> None:
        """删除临时文件，失败不影响主流程。"""
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _null():
    return _Null()


_service: Optional[VoiceCloneService] = None


def get_service(settings: Optional[Settings] = None) -> VoiceCloneService:
    """获取全局唯一的服务实例。"""
    global _service
    if _service is None:
        _service = VoiceCloneService(settings=settings)
    return _service
