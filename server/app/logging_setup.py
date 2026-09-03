"""日志配置。

统一使用中文输出，同时打印到控制台（`docker logs` 可直接查看）和
挂载到宿主机的日志文件，容器重启后日志不丢失。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志级别中文名称
LEVEL_CN = {
    logging.DEBUG: "调试",
    logging.INFO: "信息",
    logging.WARNING: "警告",
    logging.ERROR: "错误",
    logging.CRITICAL: "严重",
}

# 主日志器名称
LOGGER_NAME = "omnivoice.service"


class ChineseFormatter(logging.Formatter):
    """中文日志格式器，级别显示为中文。"""

    def __init__(self, with_color: bool = False) -> None:
        fmt = "%(asctime)s | %(levelname_cn)s | %(name_short)s | %(message)s"
        super().__init__(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")
        self.with_color = with_color

    def format(self, record: logging.LogRecord) -> str:
        record.levelname_cn = LEVEL_CN.get(record.levelno, str(record.levelno))
        name = record.name or ""
        # 只保留最后一段模块名，避免过长
        record.name_short = name.split(".")[-1] if name else "根日志器"
        return super().format(record)


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
) -> logging.Logger:
    """初始化日志系统，返回服务主日志器。

    Args:
        level: 日志级别名称（DEBUG / INFO / WARNING / ERROR）。
        log_file: 日志文件路径；为 None 时只输出到控制台。
        max_bytes: 单个日志文件最大字节数，超过后自动轮转。
        backup_count: 保留的历史日志文件个数。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False

    # 避免重复添加处理器（例如 uvicorn 重载时）
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ChineseFormatter())
    console.setLevel(logger.level)
    logger.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(ChineseFormatter())
            file_handler.setLevel(logger.level)
            logger.addHandler(file_handler)
        except OSError as exc:  # 目录不可写时降级为仅控制台
            logger.warning("无法创建日志文件 %s（原因：%s），本次仅输出到控制台。", log_file, exc)

    # 降低第三方库的日志噪音
    for noisy in ("httpx", "httpcore", "urllib3", "matplotlib", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """获取服务日志器（子模块传入 __name__ 便于定位）。"""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name.split('.')[-1]}")


def configure_uvicorn_logs(level: str = "INFO") -> None:
    """让 uvicorn / fastapi 的日志格式与本项目保持一致（中文）。"""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lib_logger = logging.getLogger(name)
        lib_logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        for handler in list(lib_logger.handlers):
            lib_logger.removeHandler(handler)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(ChineseFormatter())
        lib_logger.addHandler(handler)
        lib_logger.propagate = False
