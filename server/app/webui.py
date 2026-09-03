"""Web 页面（Gradio）。

提供两个标签页：
- 语音合成：选择已保存音色或临时上传音频，输入文本即可克隆语音；
- 音色管理：上传源音频、查看列表、删除音色、重新提取特征。

页面与接口共用 service 层，行为完全一致，并展示每个阶段的耗时。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import List, Optional

import gradio as gr

from .audio_utils import save_wav
from .logging_setup import get_logger
from .service import VoiceCloneService, get_service

logger = get_logger(__name__)

# 音色管理表格的表头
VOICE_TABLE_HEADERS = [
    "音色ID",
    "音色名称",
    "时长(秒)",
    "是否已提取特征",
    "特征提取耗时(毫秒)",
    "参考文本来源",
    "创建时间",
]


def _service() -> VoiceCloneService:
    return get_service()


def _format_timings(stages: List[dict], total_ms: float) -> str:
    """把阶段耗时渲染成 Markdown 表格。"""
    lines = ["| 阶段 | 耗时（毫秒） | 备注 |", "| --- | ---: | --- |"]
    for stage in stages:
        lines.append(
            f"| {stage['阶段']} | {stage['耗时毫秒']:.1f} | {stage['备注'] or '-'} |"
        )
    lines.append(f"| **合计** | **{total_ms:.1f}** | - |")
    return "\n".join(lines)


def _voice_choices() -> List[tuple]:
    """构造音色下拉选项：显示“名称（ID）”，值为音色 ID。"""
    try:
        voices = _service().store.list()
    except Exception as exc:
        logger.warning("获取音色列表失败：%s", exc)
        return []
    return [
        (f"{item.get('音色名称', '未命名')}（{item.get('音色ID')}）", item.get("音色ID", ""))
        for item in voices
    ]


def _voice_table() -> List[list]:
    """构造音色管理表格数据。"""
    try:
        voices = _service().store.list()
    except Exception as exc:
        logger.warning("获取音色列表失败：%s", exc)
        return []
    rows = []
    for item in voices:
        rows.append(
            [
                item.get("音色ID", ""),
                item.get("音色名称", ""),
                item.get("音频时长秒", 0.0),
                "是" if item.get("是否已提取特征") else "否",
                item.get("特征提取耗时毫秒", 0.0),
                item.get("参考文本来源", ""),
                item.get("创建时间", ""),
            ]
        )
    return rows


# ----------------------------------------------------------------------
# 回调：语音合成
# ----------------------------------------------------------------------
def _on_synthesize(
    text: str,
    source: str,
    voice_id: str,
    upload_audio: Optional[str],
    ref_text: str,
    language: str,
    num_step: float,
    guidance_scale: float,
    speed: float,
    duration: float,
    denoise: bool,
    save_as_voice: bool,
    voice_name: str,
):
    service = _service()
    if not (text or "").strip():
        return None, "", "请输入待合成的文本。"

    if source == "使用已保存音色":
        if not voice_id:
            return None, "", "请先选择一个已保存的音色，或切换到“临时上传音频”。"
        use_voice_id: Optional[str] = voice_id
        upload_path: Optional[str] = None
        upload_name = ""
    else:
        if not upload_audio:
            return None, "", "请上传一段参考音频，或切换到“使用已保存音色”。"
        use_voice_id = None
        upload_path = upload_audio
        upload_name = Path(upload_audio).name if upload_audio else ""

    try:
        result = service.synthesize(
            text=text,
            voice_id=use_voice_id,
            upload_path=upload_path,
            upload_filename=upload_name,
            ref_text=(ref_text or "").strip() or None,
            language=(language or "").strip() or None,
            duration=float(duration) if duration and float(duration) > 0 else None,
            speed=float(speed) if speed and float(speed) != 1.0 else None,
            num_step=int(num_step) if num_step else None,
            guidance_scale=float(guidance_scale) if guidance_scale else None,
            denoise=bool(denoise),
            save_as_voice=bool(save_as_voice) and upload_path is not None,
            voice_name=voice_name,
        )
    except Exception as exc:
        logger.exception("Web 页面合成失败：%s", exc)
        return None, "", f"合成失败：{exc}"

    audio_path = result.get("输出文件路径") or ""
    if not audio_path:
        # 未开启结果保存时，临时写出一份供页面播放
        audio_path = save_wav(
            _decode_wav_bytes(result["音频二进制"]),
            result["采样率"],
            service.settings.tmp_dir / f"ui_{uuid.uuid4().hex[:8]}.wav",
        )

    message = (
        f"合成成功，请求编号 {result['请求编号']}；"
        f"音频时长 {result['音频时长秒']} 秒；"
        f"是否复用已保存特征：{'是' if result['是否复用已保存特征'] else '否'}；"
        f"输出文件：{os.path.basename(audio_path)}"
    )
    return audio_path, _format_timings(result["阶段耗时"], result["总耗时毫秒"]), message


def _decode_wav_bytes(raw: bytes):
    """把 wav 二进制解码为浮点波形（页面回退播放时使用）。"""
    import io

    import soundfile as sf

    data, _ = sf.read(io.BytesIO(raw), dtype="float32")
    return data


# ----------------------------------------------------------------------
# 回调：音色管理
# ----------------------------------------------------------------------
def _on_upload_voice(file_path: Optional[str], name: str, ref_text: str, extract: bool):
    service = _service()
    if not file_path:
        return "请先选择要上传的音频文件。", [], gr.update()

    filename = Path(file_path).name
    try:
        result = service.create_voice(
            upload_path=file_path,
            filename=filename,
            name=name,
            ref_text=ref_text,
            extract=bool(extract),
        )
    except Exception as exc:
        logger.exception("Web 页面上传音色失败：%s", exc)
        return f"上传失败：{exc}", _voice_table(), gr.update(choices=_voice_choices())

    meta = result["音色信息"]
    message = (
        f"上传成功，音色ID {meta['音色ID']}，"
        f"音频时长 {meta['音频时长秒']} 秒，"
        f"是否已提取特征：{'是' if meta['是否已提取特征'] else '否'}\n\n"
        + _format_timings(result["阶段耗时"], result["总耗时毫秒"])
    )
    return message, _voice_table(), gr.update(choices=_voice_choices(), value=meta["音色ID"])


def _on_refresh():
    return gr.update(choices=_voice_choices()), _voice_table()


def _on_delete_voice(voice_id: str):
    service = _service()
    voice_id = (voice_id or "").strip()
    if not voice_id:
        return "请先填写要删除的音色ID。", _voice_table(), gr.update()
    if not service.store.delete(voice_id):
        return f"删除失败：音色不存在（{voice_id}）", _voice_table(), gr.update(choices=_voice_choices())
    return f"已删除音色：{voice_id}", _voice_table(), gr.update(choices=_voice_choices(), value=None)


def _on_reextract(voice_id: str, ref_text: str):
    service = _service()
    voice_id = (voice_id or "").strip()
    if not voice_id:
        return "请先填写要重新提取特征的音色ID。", _voice_table()

    try:
        result = service.reextract_voice(
            voice_id, ref_text=(ref_text or "").strip() or None
        )
    except Exception as exc:
        logger.exception("Web 页面重新提取特征失败：%s", exc)
        return f"重新提取失败：{exc}", _voice_table()

    message = f"特征提取完成，音色ID {voice_id}\n\n" + _format_timings(
        result["阶段耗时"], result["总耗时毫秒"]
    )
    return message, _voice_table()


# ----------------------------------------------------------------------
# 页面构建
# ----------------------------------------------------------------------
def build_ui() -> gr.Blocks:
    """构建 Gradio 页面。"""
    # 显式使用 Gradio 系统字体栈：主题若识别为非系统字体，会自动去请求
    # Google Fonts，内网环境无法访问会导致页面加载缓慢，因此这里固定为系统字体
    theme = gr.themes.Soft(font=("ui-sans-serif", "system-ui", "sans-serif"))
    with gr.Blocks(
        title="OmniVoice 语音克隆服务",
        theme=theme,
        css=".gradio-container {max-width: 1200px !important;}",
    ) as demo:
        gr.Markdown(
            """
# OmniVoice 语音克隆服务

上传一段参考音频即可克隆音色，支持 **wav / mp3 / m4a / flac / ogg** 等常见格式。
特征只需提取一次并持久化保存，后续合成直接复用，无需重复计算。
"""
        )

        with gr.Tabs():
            # ----------------------------------------------------------
            # 语音合成
            # ----------------------------------------------------------
            with gr.TabItem("语音合成"):
                with gr.Row():
                    with gr.Column(scale=3):
                        tts_text = gr.Textbox(
                            label="待合成文本",
                            lines=5,
                            placeholder="请输入要合成的文本内容 ...",
                        )
                        source_radio = gr.Radio(
                            label="音色来源",
                            choices=["使用已保存音色", "临时上传音频"],
                            value="使用已保存音色",
                        )
                        voice_dropdown = gr.Dropdown(
                            label="已保存音色",
                            choices=_voice_choices(),
                            value=None,
                        )
                        upload_audio = gr.Audio(
                            label="临时上传参考音频（wav/mp3/m4a 等）",
                            type="filepath",
                        )
                        ref_text = gr.Textbox(
                            label="参考音频文本（可选）",
                            lines=2,
                            placeholder="留空则由 ASR 自动识别参考音频内容",
                        )
                        language = gr.Textbox(
                            label="语种（可选）",
                            placeholder="例如：Chinese、English、en；留空自动判断",
                        )
                        gr.Markdown(
                            "<span style='font-size:0.85em;color:#888;'>"
                            "建议参考音频时长 3–10 秒，过长的音频会被自动裁剪。</span>"
                        )

                        with gr.Accordion("高级参数", open=False):
                            with gr.Row():
                                num_step = gr.Slider(
                                    4, 64, value=32, step=1, label="扩散步数"
                                )
                                guidance_scale = gr.Slider(
                                    0.0, 4.0, value=2.0, step=0.1, label="引导系数"
                                )
                            with gr.Row():
                                speed = gr.Slider(
                                    0.5, 1.5, value=1.0, step=0.05, label="语速倍率"
                                )
                                duration = gr.Number(
                                    value=None, label="固定时长（秒，留空不限）"
                                )
                            denoise = gr.Checkbox(value=True, label="启用降噪")
                            save_as_voice = gr.Checkbox(
                                value=False,
                                label="把本次临时上传的音频另存为音色（便于后续复用）",
                            )
                            voice_name = gr.Textbox(
                                label="另存时的音色名称（可选）", lines=1
                            )

                        tts_button = gr.Button("开始合成", variant="primary")

                    with gr.Column(scale=2):
                        output_audio = gr.Audio(label="合成结果", type="filepath")
                        output_status = gr.Textbox(label="状态", lines=3)
                        timing_md = gr.Markdown(label="阶段耗时", value="")

                tts_button.click(
                    _on_synthesize,
                    inputs=[
                        tts_text,
                        source_radio,
                        voice_dropdown,
                        upload_audio,
                        ref_text,
                        language,
                        num_step,
                        guidance_scale,
                        speed,
                        duration,
                        denoise,
                        save_as_voice,
                        voice_name,
                    ],
                    outputs=[output_audio, timing_md, output_status],
                )

            # ----------------------------------------------------------
            # 音色管理
            # ----------------------------------------------------------
            with gr.TabItem("音色管理"):
                gr.Markdown("### 上传源音频")
                with gr.Row():
                    with gr.Column(scale=1):
                        up_audio = gr.Audio(
                            label="选择音频文件", type="filepath", sources=["upload"]
                        )
                        up_name = gr.Textbox(
                            label="音色名称（可选）", placeholder="留空则使用文件名"
                        )
                        up_ref_text = gr.Textbox(
                            label="参考音频文本（可选）", lines=2
                        )
                        up_extract = gr.Checkbox(
                            value=True, label="上传后立即提取并保存特征"
                        )
                        up_button = gr.Button("上传并保存", variant="primary")
                    with gr.Column(scale=2):
                        up_status = gr.Markdown(value="")

                gr.Markdown("### 已保存的音色")
                voice_table = gr.Dataframe(
                    headers=VOICE_TABLE_HEADERS,
                    value=_voice_table(),
                    interactive=False,
                )
                with gr.Row():
                    refresh_button = gr.Button("刷新列表")

                gr.Markdown("### 删除 / 重新提取")
                with gr.Row():
                    manage_id = gr.Textbox(
                        label="音色ID", placeholder="从上方表格复制音色ID"
                    )
                    reextract_ref_text = gr.Textbox(
                        label="重新提取时的参考文本（可选）"
                    )
                with gr.Row():
                    delete_button = gr.Button("删除音色", variant="stop")
                    reextract_button = gr.Button("重新提取特征")
                manage_status = gr.Markdown(value="")

                up_button.click(
                    _on_upload_voice,
                    inputs=[up_audio, up_name, up_ref_text, up_extract],
                    outputs=[up_status, voice_table, voice_dropdown],
                )
                refresh_button.click(
                    _on_refresh, inputs=None, outputs=[voice_dropdown, voice_table]
                )
                delete_button.click(
                    _on_delete_voice,
                    inputs=[manage_id],
                    outputs=[manage_status, voice_table, voice_dropdown],
                )
                reextract_button.click(
                    _on_reextract,
                    inputs=[manage_id, reextract_ref_text],
                    outputs=[manage_status, voice_table],
                )

    return demo
