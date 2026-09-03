"""接口数据模型（请求体）。

请求字段使用英文键，方便各类客户端调用；接口返回的说明性内容统一使用中文。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TTSJsonRequest(BaseModel):
    """语音合成请求（JSON 版本，音频以 base64 传入）。"""

    text: str = Field(..., description="待合成的文本，必填")
    voice_id: Optional[str] = Field(None, description="已保存的音色 ID，与 audio_base64 二选一")
    audio_base64: Optional[str] = Field(None, description="临时上传音频的 base64 内容")
    audio_format: Optional[str] = Field("wav", description="临时上传音频的格式，如 wav / mp3 / m4a")
    ref_text: Optional[str] = Field(None, description="参考音频对应的文本，留空则用 ASR 自动识别")
    language: Optional[str] = Field(None, description="语种名称或代码，留空自动判断")
    instruct: Optional[str] = Field(None, description="声音设计描述，如 female, low pitch")
    duration: Optional[float] = Field(None, description="固定输出时长（秒），设置后忽略 speed")
    speed: Optional[float] = Field(None, description="语速倍率，大于 1 更快")
    num_step: Optional[int] = Field(None, description="扩散步数，默认 32")
    guidance_scale: Optional[float] = Field(None, description="CFG 引导系数，默认 2.0")
    denoise: bool = Field(True, description="是否启用降噪")
    preprocess_prompt: bool = Field(True, description="是否对参考音频做静音裁剪等预处理")
    postprocess_output: bool = Field(True, description="是否对输出音频做后处理")
    save_as_voice: bool = Field(False, description="是否把本次临时上传的音频保存为新音色")
    voice_name: str = Field("", description="保存为新音色时的音色名称")
    response_format: str = Field("wav", description="返回格式：wav 直接返回音频，json 返回 base64 与耗时明细")


class VoiceUpdateRequest(BaseModel):
    """音色信息修改请求。"""

    name: Optional[str] = Field(None, description="音色名称")
    ref_text: Optional[str] = Field(None, description="参考文本")


class ExtractRequest(BaseModel):
    """特征提取请求。"""

    ref_text: Optional[str] = Field(None, description="参考文本，留空则用 ASR 自动识别")
    force: bool = Field(True, description="为 true 时即使已有特征也重新提取")
