# ---------------------------------------------------------------------------
# OmniVoice 语音克隆服务镜像
#
# 构建（在仓库根目录执行）：
#     docker build -f server/Dockerfile -t omnivoice-service:latest .
#
# 国内加速构建（可选）：
#     docker build -f server/Dockerfile -t omnivoice-service:latest \
#       --build-arg USE_CN_MIRROR=true \
#       --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple .
#
# 兼容性说明：
#   - 本文件不使用任何 BuildKit 专有语法（如 --mount、heredoc、COPY --chmod），
#     Docker 18.09 及以上版本均可正常构建；
#   - 未安装 flash-attn：T4（Turing sm_75）不支持该算子，统一走 SDPA 注意力，
#     保证 T4 / A10 / A100 等多种显卡环境通用。
# ---------------------------------------------------------------------------
# 基础镜像，可通过 --build-arg CUDA_IMAGE=... 切换
# 说明：PyTorch 的 pip 包自带 cuDNN 与 CUDA 运行库，因此使用 runtime 版本即可，
# 无需 cudnn 版本；容器内 CUDA 版本无需与 torch 的 CUDA 版本严格一致，
# 真正决定能否运行的是宿主机显卡驱动所支持的最高 CUDA 版本。
ARG CUDA_IMAGE=12.4.1-runtime-ubuntu22.04
FROM nvidia/cuda:${CUDA_IMAGE}

# 是否使用国内镜像源加速 apt 下载（true / false）
ARG USE_CN_MIRROR=false
# apt 镜像源地址（USE_CN_MIRROR=true 时生效）
ARG APT_MIRROR=https://mirrors.aliyun.com/ubuntu/
# pip 源地址，留空则使用官方源
ARG PIP_INDEX_URL=

# 模型缓存目录（建议挂载到宿主机，避免重复下载）
ENV HF_HOME=/opt/models/huggingface
ENV HF_HUB_CACHE=/opt/models/huggingface
ENV TRANSFORMERS_CACHE=/opt/models/huggingface
ENV TORCH_HOME=/opt/models/torch

# 服务运行参数（均可通过 docker run -e 覆盖）
ENV DATA_DIR=/opt/omnivoice-service/data
ENV HOST=0.0.0.0
ENV PORT=8000
ENV UI_PATH=/ui
ENV API_PREFIX=/api/v1
ENV MODEL_ID=k2-fsa/OmniVoice
ENV DEVICE=
ENV DTYPE=float16
ENV ATTN_IMPLEMENTATION=
ENV PRELOAD_MODEL=true
ENV LOAD_ASR=true
ENV ASR_MODEL=openai/whisper-large-v3-turbo
ENV ASR_DEVICE=
ENV HF_ENDPOINT=
ENV HF_HUB_OFFLINE=0
ENV LOG_LEVEL=INFO
ENV LOG_TO_FILE=true
ENV DEFAULT_NUM_STEP=32
ENV DEFAULT_GUIDANCE_SCALE=2.0
ENV MAX_UPLOAD_MB=50
ENV MAX_REF_DURATION=30
ENV FEATURE_CACHE_SIZE=64
ENV ENABLE_WEBUI=true
ENV SAVE_OUTPUT=true
ENV GRADIO_ANALYTICS_ENABLED=false
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# Python 相关
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

# ---------------------------------------------------------------------------
# apt 网络容错：加大超时并自动重试，避免源站抖动导致构建中断
# ---------------------------------------------------------------------------
RUN printf 'Acquire::Retries "10";\nAcquire::http::Timeout "60";\nAcquire::https::Timeout "60";\n' \
    > /etc/apt/apt.conf.d/99-custom-retries

# ---------------------------------------------------------------------------
# 系统依赖
# ffmpeg：用于 mp3 / m4a / aac 等压缩音频格式的解码与转码
# ---------------------------------------------------------------------------
RUN if [ "$USE_CN_MIRROR" = "true" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu/|${APT_MIRROR}|g; s|http://security.ubuntu.com/ubuntu/|${APT_MIRROR}|g" /etc/apt/sources.list || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.10 \
        python3-pip \
        python3-dev \
        ffmpeg \
        libsndfile1 \
        git \
        curl \
        ca-certificates \
    && ln -sf /usr/bin/python3 /usr/bin/python \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip setuptools wheel

# ---------------------------------------------------------------------------
# pip 源配置（可选）
# 必须在 pip 安装完成后执行，否则配置不生效
# ---------------------------------------------------------------------------
RUN python3 -m pip config set global.retries 10 \
    && python3 -m pip config set global.timeout 60 \
    && if [ -n "${PIP_INDEX_URL}" ]; then \
           python3 -m pip config set global.index-url "${PIP_INDEX_URL}"; \
       fi

# ---------------------------------------------------------------------------
# PyTorch
# TORCH_CUDA 需与宿主机驱动支持的 CUDA 版本匹配（torch 2.8.0 提供 cu126/cu128/cu129）：
#   驱动较新（支持 12.8 及以上）→ 保持默认 cu128
#   驱动较老（仅支持 12.6）      → --build-arg TORCH_CUDA=cu126
# ---------------------------------------------------------------------------
ARG TORCH_VERSION=2.8.0
ARG TORCH_CUDA=cu128
RUN python3 -m pip install \
        torch==${TORCH_VERSION}+${TORCH_CUDA} \
        torchaudio==${TORCH_VERSION}+${TORCH_CUDA} \
        --extra-index-url https://download.pytorch.org/whl/${TORCH_CUDA}

# ---------------------------------------------------------------------------
# 安装 OmniVoice 本体
# ---------------------------------------------------------------------------
COPY pyproject.toml README.md /opt/OmniVoice/
COPY omnivoice /opt/OmniVoice/omnivoice/
RUN cd /opt/OmniVoice && python3 -m pip install .

# ---------------------------------------------------------------------------
# 安装服务依赖并部署服务代码
# ---------------------------------------------------------------------------
COPY server /opt/omnivoice-service
WORKDIR /opt/omnivoice-service
RUN python3 -m pip install -r requirements.txt

# 数据、日志、模型缓存统一放在可挂载目录中，保证容器重启后不丢失
VOLUME ["/opt/omnivoice-service/data", "/opt/models"]

EXPOSE 8000

# 启动脚本：准备目录、打印中文环境信息、然后执行主程序
COPY server/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

HEALTHCHECK --interval=60s --timeout=15s --start-period=300s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "app.main"]
