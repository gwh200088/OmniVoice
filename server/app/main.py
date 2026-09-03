"""服务入口。

启动 FastAPI（HTTP 接口）+ Gradio（Web 页面），并在启动时按需预加载模型。
所有日志均为中文，接口访问与每个业务阶段都会打印耗时。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import router as api_router
from .config import get_settings
from .engine import get_engine
from .logging_setup import configure_uvicorn_logs, get_logger, setup_logging

# 先初始化日志，后续所有模块都能直接使用
settings = get_settings()
logger = setup_logging(
    level=settings.log_level,
    log_file=(settings.logs_dir / "service.log") if settings.log_to_file else None,
    max_bytes=settings.log_max_bytes,
    backup_count=settings.log_backup_count,
)

DESCRIPTION = """
基于 **OmniVoice** 的语音克隆服务，提供 Web 页面与 HTTP 接口两种使用方式。

**能力一览**

- 上传 wav / mp3 / m4a / flac / ogg 等常见格式音频，克隆其音色；
- 支持使用已保存音色（直接复用特征）或每次临时上传音频；
- 源音频与提取出的特征均持久化保存，容器重启后无需重算；
- 全流程中文日志，逐阶段打印耗时，便于定位性能瓶颈；
- 支持 T4 / A10 / A100 等 NVIDIA 显卡，Docker 一键部署。

**调用方式**：本页接口统一以 `/api/v1` 为前缀，Web 页面位于 `/ui`。
"""

TAGS_METADATA = [
    {"name": "语音克隆服务", "description": "音色管理与语音合成接口"},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务生命周期：启动时预加载模型，退出时打印收尾日志。"""
    logger.info("=" * 70)
    logger.info("OmniVoice 语音克隆服务启动中，版本 %s", __version__)
    logger.info("配置摘要：%s", settings.summary())

    if settings.preload_model:
        try:
            get_engine(settings).load()
        except Exception as exc:
            logger.error("模型预加载失败：%s", exc)
            if settings.hf_hub_offline:
                # 离线模式下配置错误无法自动恢复，直接退出避免"启动成功却不可用"
                logger.error(
                    "离线模式下模型加载失败，服务无法正常工作，正在退出。"
                    "请检查模型挂载路径、MODEL_ID 与 ASR_MODEL 配置。"
                )
                raise
            logger.error("将在首次请求时重试。")
    else:
        logger.info("已关闭模型预加载，首次请求时自动加载模型。")

    logger.info("Web 页面地址：http://%s:%s%s", settings.host, settings.port, settings.ui_path)
    logger.info("接口文档地址：http://%s:%s/docs", settings.host, settings.port)
    logger.info("=" * 70)
    yield
    logger.info("服务正在关闭，感谢使用。")


app = FastAPI(
    title="OmniVoice 语音克隆服务",
    description=DESCRIPTION,
    version=__version__,
    lifespan=lifespan,
    openapi_tags=TAGS_METADATA,
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """记录每个接口的访问信息与耗时（中文）。"""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:  # 统一兜底，避免异常穿透导致无响应
        elapsed = (time.perf_counter() - started) * 1000.0
        logger.error(
            "接口访问异常：%s %s，耗时 %.1f 毫秒，错误：%s",
            request.method,
            request.url.path,
            elapsed,
            exc,
        )
        return JSONResponse(status_code=500, content={"提示": f"服务内部错误：{exc}"})

    elapsed = (time.perf_counter() - started) * 1000.0
    logger.info(
        "接口访问：%s %s，状态码=%d，耗时 %.1f 毫秒",
        request.method,
        request.url.path,
        response.status_code,
        elapsed,
    )
    return response


@app.get("/", summary="服务首页", description="返回服务基本信息与常用入口。")
def index() -> dict:
    return {
        "服务名称": "OmniVoice 语音克隆服务",
        "版本": __version__,
        "Web 页面": settings.ui_path,
        "接口文档": "/docs",
        "健康检查": f"{settings.api_prefix}/health",
        "服务信息": f"{settings.api_prefix}/info",
        "说明": "模型若未预加载，首次请求时会自动加载，可能需要等待数十秒。",
    }


# 注册业务接口
app.include_router(api_router, prefix=settings.api_prefix, tags=["语音克隆服务"])

# 合成结果支持直接通过 URL 下载
app.mount(
    "/outputs",
    StaticFiles(directory=str(settings.outputs_dir)),
    name="outputs",
)

# 挂载 Web 页面（可通过 ENABLE_WEBUI=false 关闭）
if settings.enable_webui:
    try:
        import gradio as gr

        from .webui import build_ui

        app = gr.mount_gradio_app(app, build_ui(), path=settings.ui_path)
        logger.info("Web 页面已挂载，访问路径：%s", settings.ui_path)
    except Exception as exc:
        logger.error("Web 页面挂载失败，仅提供 HTTP 接口。失败原因：%s", exc)


def main() -> None:
    """命令行入口：python -m app.main"""
    configure_uvicorn_logs(settings.log_level)
    uvicorn_config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        root_path=settings.root_path or "",
        log_config=None,  # 使用项目自身的中文日志配置
        access_log=False,  # 访问日志由中间件统一输出
    )
    server = uvicorn.Server(uvicorn_config)
    server.run()


if __name__ == "__main__":
    main()
