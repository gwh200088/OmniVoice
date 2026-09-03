"""服务配置。

所有配置项都可以通过环境变量覆盖，因此同一份镜像可以不加改动地部署到
T4 / A10 / A100 等不同显卡环境，只需在 `docker run -e KEY=VALUE` 中调整。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# server/ 目录（即 app 包的上级目录）
BASE_DIR = Path(__file__).resolve().parents[1]


def env_str(name: str, default: str = "") -> str:
    """读取字符串环境变量，未设置或为空时返回默认值。"""
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


def env_int(name: str, default: int) -> int:
    """读取整数环境变量，解析失败时返回默认值。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    """读取浮点数环境变量，解析失败时返回默认值。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔环境变量，支持 1/true/yes/y/on 等写法。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "t"}


def env_list(name: str, default: List[str]) -> List[str]:
    """读取逗号分隔的列表环境变量。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    """服务运行参数。"""

    # ---------------- 服务 ----------------
    host: str = "0.0.0.0"
    port: int = 8000
    ui_path: str = "/ui"
    api_prefix: str = "/api/v1"
    root_path: str = ""
    enable_webui: bool = True

    # ---------------- 模型 ----------------
    model_id: str = "k2-fsa/OmniVoice"
    device: str = ""
    dtype: str = "float16"
    attn_implementation: str = ""
    preload_model: bool = True
    load_asr: bool = True
    asr_model: str = "openai/whisper-large-v3-turbo"
    asr_device: str = ""
    hf_endpoint: str = ""

    # ---------------- 数据目录 ----------------
    data_dir: Path = field(default_factory=lambda: Path(env_str("DATA_DIR", str(BASE_DIR / "data"))))
    target_sample_rate: int = 24000

    # ---------------- 音频上传 ----------------
    max_upload_mb: int = 50
    max_ref_duration: float = 30.0
    allowed_extensions: List[str] = field(default_factory=list)

    # ---------------- 特征缓存 ----------------
    feature_cache_size: int = 64

    # ---------------- 日志 ----------------
    log_level: str = "INFO"
    log_to_file: bool = True
    log_max_bytes: int = 20 * 1024 * 1024
    log_backup_count: int = 10

    # ---------------- 合成默认值 ----------------
    default_num_step: int = 32
    default_guidance_scale: float = 2.0
    save_output: bool = True

    def __post_init__(self) -> None:
        # 目录全部收敛到 data_dir 下，方便整体挂载到宿主机做持久化
        self.voices_dir = self.data_dir / "voices"
        self.outputs_dir = self.data_dir / "outputs"
        self.logs_dir = self.data_dir / "logs"
        self.tmp_dir = self.data_dir / "tmp"

        for directory in (
            self.data_dir,
            self.voices_dir,
            self.outputs_dir,
            self.logs_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        if not self.allowed_extensions:
            self.allowed_extensions = [
                "wav", "mp3", "m4a", "aac", "flac", "ogg", "oga",
                "opus", "wma", "webm", "aiff", "aif", "amr", "mp4",
            ]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def summary(self) -> str:
        """返回配置摘要（用于启动日志，避免逐项打印）。"""
        return (
            f"服务监听地址={self.host}:{self.port}；"
            f"Web 页面路径={self.ui_path}；"
            f"模型={self.model_id}；"
            f"推理设备={self.device or '自动检测'}；"
            f"计算精度={self.dtype}；"
            f"注意力实现={self.attn_implementation or '自动选择'}；"
            f"ASR 自动识别={'开启' if self.load_asr else '关闭'}；"
            f"数据目录={self.data_dir}；"
            f"特征缓存条数={self.feature_cache_size}；"
            f"日志级别={self.log_level}"
        )


def load_settings() -> Settings:
    """从环境变量构建配置对象。"""
    return Settings(
        # 服务
        host=env_str("HOST", "0.0.0.0"),
        port=env_int("PORT", 8000),
        ui_path=env_str("UI_PATH", "/ui"),
        api_prefix=env_str("API_PREFIX", "/api/v1"),
        root_path=env_str("ROOT_PATH", ""),
        enable_webui=env_bool("ENABLE_WEBUI", True),
        # 模型
        model_id=env_str("MODEL_ID", "k2-fsa/OmniVoice"),
        device=env_str("DEVICE", ""),
        dtype=env_str("DTYPE", "float16"),
        attn_implementation=env_str("ATTN_IMPLEMENTATION", ""),
        preload_model=env_bool("PRELOAD_MODEL", True),
        load_asr=env_bool("LOAD_ASR", True),
        asr_model=env_str("ASR_MODEL", "openai/whisper-large-v3-turbo"),
        asr_device=env_str("ASR_DEVICE", ""),
        hf_endpoint=env_str("HF_ENDPOINT", ""),
        # 数据
        target_sample_rate=env_int("TARGET_SAMPLE_RATE", 24000),
        # 音频
        max_upload_mb=env_int("MAX_UPLOAD_MB", 50),
        max_ref_duration=env_float("MAX_REF_DURATION", 30.0),
        allowed_extensions=env_list(
            "ALLOWED_EXTENSIONS",
            [
                "wav", "mp3", "m4a", "aac", "flac", "ogg", "oga",
                "opus", "wma", "webm", "aiff", "aif", "amr", "mp4",
            ],
        ),
        # 缓存
        feature_cache_size=env_int("FEATURE_CACHE_SIZE", 64),
        # 日志
        log_level=env_str("LOG_LEVEL", "INFO").upper(),
        log_to_file=env_bool("LOG_TO_FILE", True),
        log_max_bytes=env_int("LOG_MAX_BYTES", 20 * 1024 * 1024),
        log_backup_count=env_int("LOG_BACKUP_COUNT", 10),
        # 合成默认
        default_num_step=env_int("DEFAULT_NUM_STEP", 32),
        default_guidance_scale=env_float("DEFAULT_GUIDANCE_SCALE", 2.0),
        save_output=env_bool("SAVE_OUTPUT", True),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局唯一配置（首次调用时加载）。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
