"""OmniVoice 语音克隆服务。

提供 Web 页面与 HTTP 接口两种使用方式，支持上传任意常见格式音频
（wav / mp3 / m4a / flac 等）提取声纹特征并复用于语音合成。
"""

import os

# 离线开关必须在导入 transformers / huggingface_hub 之前生效：
# 这两个库在导入时就会读取 HF_HUB_OFFLINE，之后再设置将不起作用。
# 本模块是 app 包的初始化模块，会先于任何子模块（进而先于 omnivoice）被执行。
if os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

__version__ = "1.0.0"
