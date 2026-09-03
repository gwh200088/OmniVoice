#!/bin/sh
# 容器启动脚本：准备持久化目录、打印环境信息（中文）、启动服务。
set -e

DATA_DIR="${DATA_DIR:-/opt/omnivoice-service/data}"

echo "============================================================"
echo " OmniVoice 语音克隆服务"
echo "============================================================"
echo "数据目录：${DATA_DIR}"
echo "模型标识：${MODEL_ID:-k2-fsa/OmniVoice}"
echo "推理设备：${DEVICE:-自动检测}"
echo "计算精度：${DTYPE:-float16}"
echo "服务端口：${PORT:-8000}"
echo "Web 页面：http://0.0.0.0:${PORT:-8000}${UI_PATH:-/ui}"
echo "============================================================"

# 创建持久化目录（挂载目录权限不足时给出中文提示）
mkdir -p "${DATA_DIR}/voices" "${DATA_DIR}/outputs" "${DATA_DIR}/logs" "${DATA_DIR}/tmp" 2>/dev/null \
    || echo "警告：无法创建数据目录 ${DATA_DIR}，请检查宿主机挂载路径的权限。"

mkdir -p "${HF_HOME:-/opt/models/huggingface}" 2>/dev/null || true

if command -v ffmpeg >/dev/null 2>&1; then
    echo "音频转码组件：ffmpeg 已就绪（支持 wav / mp3 / m4a / flac / ogg 等格式）"
else
    echo "警告：未检测到 ffmpeg，mp3 / m4a 等压缩格式可能无法解码。"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
    echo "显卡信息："
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null \
        || echo "（nvidia-smi 调用失败，请确认是否已正确配置 NVIDIA Container Toolkit）"
else
    echo "警告：容器内未检测到 nvidia-smi，将以 CPU 模式运行（推理会非常慢）。"
    echo "      请使用 --gpus all（Docker 19.03+）或 --runtime=nvidia（Docker 18.09）启动容器。"
fi

echo "============================================================"
echo "正在启动服务 ..."
echo "============================================================"

exec "$@"
