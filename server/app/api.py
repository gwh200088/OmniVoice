"""HTTP 接口层。

对外提供两类能力：
1. 音色（源音频）管理：上传、列表、详情、修改、删除、特征重新提取；
2. 语音合成：既可以使用已保存的音色（直接复用特征），也可以临时上传音频。
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import Settings, get_settings
from .logging_setup import get_logger
from .schemas import ExtractRequest, TTSJsonRequest, VoiceUpdateRequest
from .service import VoiceCloneService, get_service

logger = get_logger(__name__)

router = APIRouter()


def _settings() -> Settings:
    return get_settings()


def _service() -> VoiceCloneService:
    return get_service()


async def _save_upload(upload: UploadFile, settings: Settings) -> str:
    """把上传内容落盘到临时目录，返回临时文件路径。"""
    suffix = Path(upload.filename or "").suffix
    path = settings.tmp_dir / f"upload_{uuid.uuid4().hex[:12]}{suffix}"
    max_bytes = settings.max_upload_bytes
    written = 0

    try:
        with open(path, "wb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"上传的音频超过大小上限 {settings.max_upload_mb} MB。"
                    )
                handle.write(chunk)
    except Exception:
        if path.exists():
            path.unlink(missing_ok=True)
        raise

    if written <= 0:
        path.unlink(missing_ok=True)
        raise ValueError("上传的音频文件为空。")
    return str(path)


def _safe_remove(path: Optional[str]) -> None:
    """删除请求过程中产生的临时文件，失败不影响主流程。"""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _error_response(exc: Exception) -> HTTPException:
    """把业务异常转换为对应的 HTTP 错误。"""
    if isinstance(exc, ValueError):
        logger.warning("请求参数不合法：%s", exc)
        return HTTPException(status_code=400, detail=f"请求参数错误：{exc}")
    if isinstance(exc, KeyError):
        message = str(exc).strip("'\"")
        logger.warning("资源不存在：%s", message)
        return HTTPException(status_code=404, detail=f"资源不存在：{message}")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=f"文件不存在：{exc}")
    logger.exception("处理请求时发生未预期的错误：%s", exc)
    return HTTPException(status_code=500, detail=f"服务内部错误：{exc}")


def _build_tts_response(result: dict, response_format: str) -> Response:
    """按请求的返回格式组装响应。"""
    timings = result["阶段耗时"]
    headers = {
        "X-Request-Id": result["请求编号"],
        "X-Total-Time-Ms": str(result["总耗时毫秒"]),
        "X-Audio-Duration-Sec": str(result["音频时长秒"]),
        "X-Voice-Id": result["使用的音色ID"] or "-",
        "X-Feature-Reused": "true" if result["是否复用已保存特征"] else "false",
    }

    if str(response_format).lower() == "json":
        return JSONResponse(
            status_code=200,
            content={
                "请求编号": result["请求编号"],
                "提示": "合成成功",
                "音频格式": "wav",
                "采样率": result["采样率"],
                "音频时长秒": result["音频时长秒"],
                "音频Base64": base64.b64encode(result["音频二进制"]).decode("ascii"),
                "使用的音色ID": result["使用的音色ID"],
                "是否复用已保存特征": result["是否复用已保存特征"],
                "输出文件路径": result["输出文件路径"],
                "阶段耗时": timings,
                "总耗时毫秒": result["总耗时毫秒"],
            },
            headers=headers,
        )

    return Response(
        content=result["音频二进制"],
        media_type="audio/wav",
        headers=headers,
    )


# ----------------------------------------------------------------------
# 健康检查与运行信息
# ----------------------------------------------------------------------
@router.get("/health", summary="健康检查", description="返回服务是否可用、模型是否已加载。")
def health() -> dict:
    service = _service()
    return {
        "状态": "正常",
        "模型已加载": service.engine.is_loaded,
        "推理设备": service.engine.device or "未加载",
        "数据目录": str(service.settings.data_dir),
    }


@router.get("/info", summary="服务信息", description="返回模型、设备、存储等运行信息。")
def info() -> dict:
    service = _service()
    return {
        "服务版本": "1.0.0",
        "引擎信息": service.engine.info(),
        "存储统计": service.store.stats(),
        "上传限制": {
            "单个文件最大MB": service.settings.max_upload_mb,
            "参考音频最大秒数": service.settings.max_ref_duration,
            "支持的音频格式": service.settings.allowed_extensions,
        },
        "合成默认参数": {
            "扩散步数": service.settings.default_num_step,
            "引导系数": service.settings.default_guidance_scale,
            "采样率": service.settings.target_sample_rate,
        },
    }


# ----------------------------------------------------------------------
# 音色管理
# ----------------------------------------------------------------------
@router.get("/voices", summary="音色列表", description="列出所有已上传并保存的源音频。")
def list_voices() -> dict:
    service = _service()
    voices = service.store.list()
    return {"总数": len(voices), "音色列表": voices}


@router.post("/voices", summary="上传源音频", description="上传音频并登记为音色，可立即提取声纹特征保存。")
async def create_voice(
    file: UploadFile = File(..., description="音频文件，支持 wav/mp3/m4a/flac 等"),
    name: str = Form("", description="音色名称，留空则使用文件名"),
    ref_text: str = Form("", description="参考音频文本，留空则用 ASR 自动识别"),
    extract: bool = Form(True, description="是否立即提取特征并保存"),
) -> dict:
    service = _service()
    tmp_path = None
    try:
        tmp_path = await _save_upload(file, service.settings)
        result = service.create_voice(
            upload_path=tmp_path,
            filename=file.filename or "upload",
            name=name,
            ref_text=ref_text,
            extract=extract,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error_response(exc)
    finally:
        _safe_remove(tmp_path)
    return {
        "请求编号": result["请求编号"],
        "提示": "音色创建成功",
        "音色信息": result["音色信息"],
        "阶段耗时": result["阶段耗时"],
        "总耗时毫秒": result["总耗时毫秒"],
    }


@router.get("/voices/{voice_id}", summary="音色详情", description="查看指定音色的元数据与特征状态。")
def get_voice(voice_id: str) -> dict:
    service = _service()
    meta = service.store.get(voice_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    return {"音色信息": meta}


@router.patch("/voices/{voice_id}", summary="修改音色信息", description="修改音色名称或参考文本。")
def update_voice(voice_id: str, payload: VoiceUpdateRequest) -> dict:
    service = _service()
    try:
        service.store.require(voice_id)
        meta = service.store.update_meta(
            voice_id, 音色名称=payload.name, 参考文本=payload.ref_text
        )
    except Exception as exc:
        raise _error_response(exc)
    return {"提示": "音色信息已更新", "音色信息": meta}


@router.delete("/voices/{voice_id}", summary="删除音色", description="删除音色及其源音频、特征文件。")
def delete_voice(voice_id: str) -> dict:
    service = _service()
    removed = service.store.delete(voice_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    return {"提示": "音色已删除", "音色ID": voice_id}


@router.post(
    "/voices/{voice_id}/extract",
    summary="提取或重新提取特征",
    description="为已上传的源音频提取声纹特征并保存到磁盘。",
)
def extract_voice(voice_id: str, payload: ExtractRequest) -> dict:
    service = _service()
    try:
        meta = service.store.require(voice_id)
        if meta.get("是否已提取特征") and not payload.force:
            return {
                "提示": "该音色已存在特征，未重复提取（如需强制重算请设置 force=true）",
                "音色信息": meta,
            }
        result = service.reextract_voice(voice_id, ref_text=payload.ref_text)
    except Exception as exc:
        raise _error_response(exc)
    return {
        "请求编号": result["请求编号"],
        "提示": "特征提取完成",
        "音色信息": result["音色信息"],
        "阶段耗时": result["阶段耗时"],
        "总耗时毫秒": result["总耗时毫秒"],
    }


@router.get("/voices/{voice_id}/audio", summary="下载源音频", description="下载规范化后的源音频文件（wav）。")
def download_voice_audio(voice_id: str):
    service = _service()
    meta = service.store.get(voice_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"音色不存在：{voice_id}")
    audio_path = Path(meta.get("源音频文件路径") or service.store.audio_path(voice_id))
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="源音频文件不存在")
    return FileResponse(
        str(audio_path),
        media_type="audio/wav",
        filename=f"{voice_id}.wav",
    )


# ----------------------------------------------------------------------
# 语音合成
# ----------------------------------------------------------------------
@router.post("/tts", summary="语音合成（文件上传）", description="支持使用已保存音色，或临时上传一段音频进行克隆。")
async def tts(
    text: str = Form(..., description="待合成的文本"),
    voice_id: Optional[str] = Form(None, description="已保存的音色 ID，与 file 二选一"),
    file: Optional[UploadFile] = File(None, description="临时上传的参考音频，与 voice_id 二选一"),
    ref_text: Optional[str] = Form(None, description="参考音频文本，留空自动识别"),
    language: Optional[str] = Form(None, description="语种，如 Chinese / English / en"),
    instruct: Optional[str] = Form(None, description="声音设计描述"),
    duration: Optional[float] = Form(None, description="固定输出时长（秒）"),
    speed: Optional[float] = Form(None, description="语速倍率"),
    num_step: Optional[int] = Form(None, description="扩散步数，默认 32"),
    guidance_scale: Optional[float] = Form(None, description="CFG 引导系数，默认 2.0"),
    denoise: bool = Form(True, description="是否降噪"),
    preprocess_prompt: bool = Form(True, description="参考音频预处理"),
    postprocess_output: bool = Form(True, description="输出音频后处理"),
    save_as_voice: bool = Form(False, description="是否把临时上传的音频保存为新音色"),
    voice_name: str = Form("", description="保存为新音色时的名称"),
    response_format: str = Form("wav", description="wav 返回音频，json 返回 base64 与耗时明细"),
) -> Response:
    service = _service()
    tmp_path: Optional[str] = None
    try:
        if file is not None:
            tmp_path = await _save_upload(file, service.settings)
        result = service.synthesize(
            text=text,
            voice_id=voice_id or None,
            upload_path=tmp_path,
            upload_filename=file.filename if file is not None else "",
            ref_text=ref_text,
            language=language,
            instruct=instruct,
            duration=duration,
            speed=speed,
            num_step=num_step,
            guidance_scale=guidance_scale,
            denoise=denoise,
            preprocess_prompt=preprocess_prompt,
            postprocess_output=postprocess_output,
            save_as_voice=save_as_voice,
            voice_name=voice_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error_response(exc)
    finally:
        _safe_remove(tmp_path)
    return _build_tts_response(result, response_format)


@router.post("/tts/base64", summary="语音合成（JSON/base64）", description="以 JSON + base64 方式提交，便于无文件上传能力的客户端调用。")
def tts_base64(payload: TTSJsonRequest) -> Response:
    service = _service()
    tmp_path: Optional[str] = None
    try:
        if payload.audio_base64:
            if payload.voice_id:
                raise ValueError("voice_id 与 audio_base64 只能二选一。")
            raw = base64.b64decode(payload.audio_base64)
            if not raw:
                raise ValueError("audio_base64 内容为空。")
            if len(raw) > service.settings.max_upload_bytes:
                raise ValueError(
                    f"音频超过大小上限 {service.settings.max_upload_mb} MB。"
                )
            suffix = f".{(payload.audio_format or 'wav').lstrip('.').lower()}"
            tmp_file = service.settings.tmp_dir / f"upload_{uuid.uuid4().hex[:12]}{suffix}"
            tmp_file.write_bytes(raw)
            tmp_path = str(tmp_file)

        result = service.synthesize(
            text=payload.text,
            voice_id=payload.voice_id,
            upload_path=tmp_path,
            upload_filename=f"audio.{payload.audio_format or 'wav'}",
            ref_text=payload.ref_text,
            language=payload.language,
            instruct=payload.instruct,
            duration=payload.duration,
            speed=payload.speed,
            num_step=payload.num_step,
            guidance_scale=payload.guidance_scale,
            denoise=payload.denoise,
            preprocess_prompt=payload.preprocess_prompt,
            postprocess_output=payload.postprocess_output,
            save_as_voice=payload.save_as_voice,
            voice_name=payload.voice_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error_response(exc)
    finally:
        _safe_remove(tmp_path)
    return _build_tts_response(result, payload.response_format)


@router.get("/outputs", summary="合成结果列表", description="列出已保存到本地的合成音频文件。")
def list_outputs(limit: int = Query(50, ge=1, le=500, description="返回条数上限")) -> dict:
    service = _service()
    outputs_dir: Path = service.settings.outputs_dir
    if not outputs_dir.exists():
        return {"总数": 0, "文件列表": []}
    files = sorted(
        outputs_dir.glob("*.wav"), key=lambda item: item.stat().st_mtime, reverse=True
    )[:limit]
    return {
        "总数": len(files),
        "文件列表": [
            {
                "文件名": item.name,
                "大小字节": item.stat().st_size,
                "访问地址": f"/outputs/{item.name}",
            }
            for item in files
        ],
    }


@router.get("/outputs/{filename}", summary="下载合成结果", description="按文件名下载已保存的合成音频。")
def download_output(filename: str):
    service = _service()
    target = (service.settings.outputs_dir / Path(filename).name).resolve()
    if not str(target).startswith(str(service.settings.outputs_dir.resolve())):
        raise HTTPException(status_code=400, detail="文件名不合法")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在：{filename}")
    return FileResponse(str(target), media_type="audio/wav", filename=target.name)
