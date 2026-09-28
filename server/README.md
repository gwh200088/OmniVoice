# OmniVoice 语音克隆服务

基于 [OmniVoice](https://github.com/k2-fsa/OmniVoice) 封装的语音克隆服务，提供 **Web 页面** 与 **HTTP 接口** 两种使用方式，支持 Docker 一键部署到 T4 / A10 等显卡环境。

## 功能特性

| 需求 | 实现方式 |
| --- | --- |
| 网页上传多种格式音频并克隆 | WebUI 上传组件支持 wav / mp3 / m4a / flac / ogg / aac 等，容器内由 ffmpeg 统一转码 |
| 接口调用，支持已保存音色或临时上传 | `POST /api/v1/tts` 二选一：`voice_id`（复用已存音色）或 `file`（临时上传） |
| 源音频管理与特征复用 | 每段音频登记为一个"音色"，提取 `VoiceClonePrompt` 后落盘为 `prompt.pt`，后续直接复用 |
| 多环境 Docker 部署 | 单镜像适配 T4 / A10 / A100；Dockerfile 不使用 BuildKit 语法，兼容 Docker 18.09；只用 `docker run`，无需 compose |
| 特征文件挂载持久化 | 数据目录（源音频 + 特征 + 日志 + 合成结果）统一放 `DATA_DIR`，整体挂载到宿主机 |
| 各阶段耗时日志 | 全流程中文日志，逐阶段打印耗时（转码 / 特征提取 / 合成 / 编码等），接口可返回耗时明细 |

## 目录结构

```
server/
├── Dockerfile                 # 镜像构建文件（兼容 Docker 18.09）
├── docker-entrypoint.sh       # 容器启动脚本
├── requirements.txt           # 服务自身依赖
└── app/
    ├── main.py                # 服务入口（FastAPI + Gradio）
    ├── api.py                 # HTTP 接口
    ├── webui.py               # Web 页面
    ├── service.py             # 业务编排（上传/提取/合成全流程）
    ├── engine.py              # 模型推理引擎（加载/提取/合成）
    ├── voice_store.py         # 源音频与特征存储（JSON 元数据 + .pt 特征）
    ├── audio_utils.py         # 音频转码与探测（ffmpeg）
    ├── timing.py              # 阶段耗时统计
    ├── logging_setup.py       # 中文日志配置
    ├── schemas.py             # 接口请求模型
    └── config.py              # 配置（全部支持环境变量覆盖）
```

## 数据目录（建议整体挂载）

```
data/
├── voices/<音色ID>/
│   ├── source.wav     # 规范化后的源音频（单声道 / 24kHz / 16bit）
│   ├── prompt.pt      # 提取好的声纹特征，容器重启后直接复用
│   └── meta.json      # 音色元数据（时长、参考文本、提取耗时等）
├── outputs/           # 合成结果 wav
├── logs/service.log   # 中文运行日志（按大小自动轮转）
└── tmp/               # 临时文件
```

---

## 一、构建镜像

在**仓库根目录**执行：

```bash
docker build -f server/Dockerfile -t omnivoice-service:latest .
```

国内网络加速（可选）：

```bash
docker build -f server/Dockerfile -t omnivoice-service:latest \
  --build-arg USE_CN_MIRROR=true \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple .
```

> 如需其他 CUDA 版本（例如驱动较老的机器），可通过 `--build-arg CUDA_IMAGE=12.4.1-cudnn-runtime-ubuntu22.04` 指定。

### 1.1 增量构建（只更新代码，不重装依赖）

完整构建需要下载 PyTorch 等大体积依赖（数十分钟）。**后续只改了服务代码或运行参数时，
可用增量方式基于已有镜像重建，通常几十秒内完成**：

```bash
# 基于本地已有的 omnivoice-service:latest 构建
docker build -f server/Dockerfile.quick -t omnivoice-service:quick .

# 确认无误后替换正式标签
docker tag omnivoice-service:quick omnivoice-service:latest
```

适用与不适用的场景：

| 场景 | 用哪个文件 |
| --- | --- |
| 只改了服务代码（`server/app/*`）、文档、运行参数 | `Dockerfile.quick`（快） |
| `requirements.txt` 新增了依赖 | `Dockerfile.quick`（只装新增部分） |
| 首次构建、换 CUDA/torch 版本、改系统依赖 | `Dockerfile`（完整构建） |

> 说明：主 `Dockerfile` 已按"稳定层在前、易变层在后"排列——系统依赖与 PyTorch 在前，
> 服务代码居中，运行参数 `ENV` 放在最后。因此调整运行参数时，
> 即使走完整构建流程，也只重建最后几层，不会重新下载依赖。

## 二、启动容器

### Docker 19.03 及以上（推荐）

```bash
docker run -d --name omnivoice \
  --gpus '"device=0"' \
  -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/omnivoice/models:/opt/models \
  -e DEVICE=cuda:0 \
  -e DTYPE=float16 \
  -e LOAD_ASR=true \
  omnivoice-service:latest
```

### Docker 18.09（兼容模式）

Docker 18.09 不支持 `--gpus` 参数，需预先安装 `nvidia-docker2`，然后使用 `--runtime=nvidia`：

```bash
docker run -d --name omnivoice \
  --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/omnivoice/models:/opt/models \
  -e DEVICE=cuda:0 \
  -e DTYPE=float16 \
  omnivoice-service:latest
```

> 说明：Docker 18.09 环境下请先安装 NVIDIA Container Toolkit（nvidia-docker2）：
> ```bash
> distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
> curl -s -L https://nvidia.github.io/nvidia-docker/gpgkey | apt-key add -
> curl -s -L https://nvidia.github.io/nvidia-docker/$distribution/nvidia-docker.list \
>      -o /etc/apt/sources.list.d/nvidia-docker.list
> apt-get update && apt-get install -y nvidia-docker2 && systemctl restart docker
> ```

### 挂载目录说明

| 容器内路径 | 用途 | 是否必须挂载 |
| --- | --- | --- |
| `/opt/omnivoice-service/data` | 源音频、特征、日志、合成结果 | **强烈建议**（否则容器删除后数据丢失） |
| `/opt/models` | HuggingFace 模型缓存 | 建议（避免每次重建容器重复下载模型） |

常用运维命令：

```bash
docker logs -f omnivoice          # 查看中文运行日志
docker inspect --format '{{.State.Health.Status}}' omnivoice   # 查看健康状态
docker stop omnivoice && docker start omnivoice   # 重启后特征与音频仍在
```

---

## 三、内网离线部署（无外网环境）

服务在启动时**默认会尝试联网下载模型**，内网环境必须提前准备好全部模型文件并挂载进容器。

### 3.1 需要准备哪些文件

| 文件 | 来源 | 是否必须 |
| --- | --- | --- |
| 主模型 | `k2-fsa/OmniVoice` | 必须 |
| 音频分词器 | `eustlb/higgs-audio-v2-tokenizer` | 主模型目录中若无 `audio_tokenizer` 子目录，则必须单独准备 |
| ASR 模型 | `openai/whisper-large-v3-turbo` | 开启 `LOAD_ASR=true` 时必须 |

> ⚠️ **最容易踩的坑**：OmniVoice 在主模型目录下找不到 `audio_tokenizer` 子目录时，会自动去下载 `eustlb/higgs-audio-v2-tokenizer`。很多人只下载了主模型，结果内网启动就卡在下载上。请务必确认该子目录存在。

### 3.2 在联网机器上下载

```bash
# 主模型
huggingface-cli download k2-fsa/OmniVoice --local-dir ./models/OmniVoice

# 音频分词器（先确认主模型目录里是否已自带，没有再下载）
ls ./models/OmniVoice/audio_tokenizer
huggingface-cli download eustlb/higgs-audio-v2-tokenizer \
  --local-dir ./models/OmniVoice/audio_tokenizer

# ASR 模型（需要自动识别参考音频文本时）
huggingface-cli download openai/whisper-large-v3-turbo \
  --local-dir ./models/whisper-large-v3-turbo
```

国内网络可先执行 `export HF_ENDPOINT=https://hf-mirror.com` 加速。

最终目录形如：

```
models/
├── OmniVoice/
│   ├── config.json
│   ├── *.safetensors
│   └── audio_tokenizer/     ← 关键，缺失会导致联网
└── whisper-large-v3-turbo/
```

### 3.3 镜像导入内网

镜像在联网机器上构建好后导出，再在内网机器导入（内网无需 pip 下载）：

```bash
# 外网机器
docker save omnivoice-service:latest -o omnivoice-service.tar

# 内网机器
docker load -i omnivoice-service.tar
```

### 3.4 内网启动命令

```bash
docker run -d --name omnivoice --gpus '"device=0"' -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/omnivoice/models:/opt/models \
  -e MODEL_ID=/opt/models/OmniVoice \
  -e ASR_MODEL=/opt/models/whisper-large-v3-turbo \
  -e HF_HUB_OFFLINE=1 \
  -e DEVICE=cuda:0 -e DTYPE=float16 \
  -e DEFAULT_NUM_STEP=16 \
  omnivoice-service:latest
```

关键参数说明：

- `MODEL_ID` / `ASR_MODEL` 必须指向**容器内的本地目录**（绝对路径）
- `HF_HUB_OFFLINE=1`：禁止任何联网行为。其作用有二：一是让 transformers / huggingface_hub 走纯离线加载；二是服务启动时会做**离线预检**，任一模型文件缺失都立即报出中文错误，而不是卡在下载超时上
- 若不需要自动识别参考文本，可设 `LOAD_ASR=false`，这样就不必准备 Whisper 模型（上传音色时手动填写参考文本即可）

### 3.5 离线预检的错误提示

启动日志会打印加载模式，便于确认：

```
配置摘要：...；ASR 自动识别=开启；模型加载模式=离线（禁止联网）；...
离线模式预检通过：主模型、音频分词器、ASR 模型均为本地文件。
```

若文件缺失，服务会**直接退出**（离线模式下配置错误无法自动恢复，避免"启动成功却不可用"），并打印中文处置建议，例如：

```
模型预加载失败：已开启离线模式，模型目录下缺少音频分词器子目录：
/opt/models/OmniVoice/audio_tokenizer。请在联网环境下载
eustlb/higgs-audio-v2-tokenizer 并放到该位置。
离线模式下模型加载失败，服务无法正常工作，正在退出。请检查模型挂载路径、MODEL_ID 与 ASR_MODEL 配置。
```

### 3.6 其它离线注意事项

- **Web 页面**：Gradio 前端资源已打包在镜像内，无需外网；页面字体已改为系统字体栈，不会请求 Google Fonts
- **Gradio 遥测**：镜像已设置 `GRADIO_ANALYTICS_ENABLED=false`，不会向外发送统计请求
- **不要设置 `HF_ENDPOINT`**：内网环境保持为空即可

## 四、环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 服务监听地址与端口 |
| `UI_PATH` / `API_PREFIX` | `/ui` / `/api/v1` | Web 页面路径、接口前缀 |
| `MODEL_ID` | `k2-fsa/OmniVoice` | 模型标识或本地目录；内网部署必须指向容器内的绝对路径 |
| `DEVICE` | 空（自动检测） | 如 `cuda:0`、`cpu` |
| `DTYPE` | `float16` | `float16` / `bfloat16` / `float32`（CPU 自动转 float32） |
| `ATTN_IMPLEMENTATION` | 空（自动） | 注意力实现，如 `sdpa` |
| `PRELOAD_MODEL` | `true` | 启动时预加载模型（首次请求不再等待） |
| `LOAD_ASR` / `ASR_MODEL` | `true` / `openai/whisper-large-v3-turbo` | 是否加载 ASR 用于自动识别参考文本 |
| `ASR_DEVICE` | 空 | ASR 模型所在设备，如 `cuda:0` / `cpu` |
| `DEFAULT_NUM_STEP` | `32` | 默认扩散步数，调低（如 16）可显著提速、显存更省 |
| `DEFAULT_GUIDANCE_SCALE` | `2.0` | 默认 CFG 引导系数 |
| `MAX_UPLOAD_MB` | `50` | 单个音频大小上限 |
| `MAX_REF_DURATION` | `30` | 参考音频最长保留秒数（超出自动截断） |
| `FEATURE_CACHE_SIZE` | `64` | 特征内存缓存条数（LRU） |
| `MAX_CONCURRENCY` | `0` | 推理并发上限，`0` = 自动（GPU 2 / CPU 1） |
| `GPU_SLOT_MEMORY_MB` | `0` | 单次推理显存估算（MB），`0` = 自适应学习 |
| `GPU_RESERVE_MB` | `512` | 显存安全预留（MB），低于此值不再放行新推理 |
| `GPU_UTILIZATION_LIMIT` | `0` | GPU 利用率上限（%），`0` = 不限制；需容器内有 `pynvml` 才生效 |
| `SLOT_WAIT_TIMEOUT` | `300` | 等待推理槽位的超时时间（秒） |
| `SAVE_OUTPUT` | `true` | 是否保存合成结果到 `data/outputs` |
| `ENABLE_WEBUI` | `true` | 是否启用 Web 页面 |
| `LOG_LEVEL` / `LOG_TO_FILE` | `INFO` / `true` | 日志级别、是否写日志文件 |
| `DATA_DIR` | `/opt/omnivoice-service/data` | 数据根目录 |
| `HF_ENDPOINT` | 空 | HuggingFace 镜像，如 `https://hf-mirror.com`；内网环境保持为空 |
| `HF_HUB_OFFLINE` | `0` | 设为 `1` 启用离线模式：禁止联网并做启动预检，模型缺失时直接报错退出 |

## 五、不同显卡环境建议

| 显卡 | 建议参数 | 说明 |
| --- | --- | --- |
| T4（16GB，sm_75） | `DTYPE=float16` | 不支持 flash-attn，镜像未安装，自动使用 SDPA |
| A10（24GB，sm_86） | `DTYPE=float16` 或 `bfloat16` | 显存更充裕，可适当调高 `FEATURE_CACHE_SIZE` |
| A100 / H100 | `DTYPE=bfloat16` | 精度更好 |
| 无显卡（CPU 调试） | `DEVICE=cpu` `DTYPE=float32` | 可跑通流程，但速度很慢 |

显存紧张时可降低 `num_step`（如 16）、缩短参考音频（`MAX_REF_DURATION=15`）。

### 5.1 CPU 模式部署（无显卡环境）

服务可以在没有显卡的机器上运行，适用于**功能验证、接口联调**，但速度很慢，不建议用于生产。

**无需修改代码**，服务已自动适配：

- 设备自动检测：未检测到 GPU 时自动回退到 `cpu`
- 精度自动转换：CPU 模式下会强制把 `float16` / `bfloat16` 转为 `float32`
  （CPU 不支持半精度算子，否则推理会直接报错）

推荐参数：

| 参数 | GPU 建议值 | CPU 建议值 | 说明 |
| --- | --- | --- | --- |
| `DEVICE` | `cuda:0` | `cpu` | 指定使用 CPU |
| `DTYPE` | `float16` | `float32` | 代码会自动转换，显式设置可避免启动警告 |
| `DEFAULT_NUM_STEP` | `16`~`32` | `8`~`16` | 耗时与步数近似成正比，CPU 上尽量调低 |
| `MAX_REF_DURATION` | `15`~`30` | `8`~`10` | 缩短参考音频，减少特征提取耗时 |
| `LOAD_ASR` | `true` | `false` | Whisper 在 CPU 上极慢，且额外占用约 3 GB 内存 |

启动命令（**不需要 `--gpus` 参数**）：

```bash
docker run -d --name omnivoice-cpu -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/models:/opt/models \
  -e MODEL_ID=/opt/models/OmniVoice \
  -e DEVICE=cpu \
  -e DTYPE=float32 \
  -e DEFAULT_NUM_STEP=16 \
  -e MAX_REF_DURATION=10 \
  -e LOAD_ASR=false \
  -e HF_HUB_OFFLINE=1 \
  omnivoice-service:latest
```

注意事项：

- **内存要求**：模型以 float32 加载约占 6 GB，建议机器内存 **≥ 16 GB**；开启 ASR 还需额外约 3 GB
- **速度预期**：CPU 约为 T4 的 1/20 ~ 1/40。合成 23 秒音频，T4 约 8~15 秒，CPU 预计需要 **3~10 分钟**
- **线程控制**：默认占用所有 CPU 核心，如需限制可加 `-e OMP_NUM_THREADS=8`
- **关闭 ASR 后**：上传音色时需手动填写参考音频对应的文本（合成质量不受影响）

### 5.1.1 CPU 提速方案

CPU 上无法达到 GPU 的秒级响应，优化目标是把"分钟级"压缩到可接受范围。按投入从低到高分为三层。

#### 第一层：调整启动参数（无需改代码）

按收益从大到小：

| 手段 | 参数 | 预期收益 | 代价 |
| --- | --- | --- | --- |
| 降低扩散步数 | `DEFAULT_NUM_STEP=8` | 32 → 8 约省 75% 耗时（**最大杠杆**） | 音质下降，需实测找平衡点 |
| 限制线程数 | `OMP_NUM_THREADS=<物理核数>` | 约 10~30% | 无 |
| 缩短参考音频 | `MAX_REF_DURATION=8` | 特征提取省 30~50% | 参考信息变少 |
| 关闭 ASR | `LOAD_ASR=false` | 上传音色从分钟级降至秒级 | 需手动填写参考文本 |
| 复用已保存特征 | 已有机制，无需配置 | 第二次起省掉全部特征提取 | 无 |

完整示例：

```bash
docker run -d --name omnivoice-cpu -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/models:/opt/models \
  -e MODEL_ID=/opt/models/OmniVoice \
  -e DEVICE=cpu -e DTYPE=float32 \
  -e DEFAULT_NUM_STEP=8 \
  -e MAX_REF_DURATION=8 \
  -e LOAD_ASR=false \
  -e HF_HUB_OFFLINE=1 \
  -e OMP_NUM_THREADS=16 \
  omnivoice-service:latest
```

`OMP_NUM_THREADS` 建议填**物理核心数**（不是超线程数）：

```bash
nproc                                               # 查看逻辑核数
lscpu | grep -E 'Core\(s\) per socket|Socket\(s\)'  # 物理核数 = Core(s) × Socket(s)
```

**确定最佳步数的方法**：同一段文本分别用 32 / 16 / 8 合成，对比接口返回的 `语音合成` 阶段耗时，选一个"听感可接受 + 耗时可容忍"的值。经验上 12~16 是性价比拐点，8 是极限。

#### 第二层：CPU 专用加速（需额外适配）

以下两项取决于 CPU 型号，收益可能很大：

| 方案 | 适用条件 | 预期收益 | 说明 |
| --- | --- | --- | --- |
| 使用 bf16 精度 | CPU 支持 `AVX512_BF16` 或 `AMX` | 2~4 倍，且内存占用减半 | 当前 CPU 模式强制 float32，需放开精度限制 |
| Intel Extension for PyTorch（IPEX） | Intel CPU | 再快 1.5~3 倍 | 镜像内加装 `intel-extension-for-pytorch`，加载时调用 `ipex.optimize()` |

检测 CPU 指令集：

```bash
lscpu | grep -i -o -E 'amx_bf16|avx512_bf16|avx512f|avx2'
lscpu | grep -E 'Model name|Core\(s\) per socket|Socket\(s\)'
```

- 输出包含 `amx_bf16` 或 `avx512_bf16` → 可启用 bf16
- CPU 为 Intel 且较新（Sapphire Rapids 及以后）→ 可叠加 IPEX

#### 第三层：架构层面

- **批量合成**：CPU 多核在批处理时利用率更高。当前接口为单条串行，若是批量生成场景（一次性合成数百条），可增加批量接口提升吞吐
- **保持容器常驻**：CPU 上模型加载需数分钟，务必保持 `PRELOAD_MODEL=true`，避免频繁重启容器

#### 调优步骤建议

1. 先按第一层配置跑通，用 `response_format=json` 读取 `语音合成` 阶段耗时作为基线
2. 若仍不可接受，按第二层检测 CPU 指令集，具备条件则启用 bf16 / IPEX
3. 若最终目标是 GPU 生产环境，CPU 调优够用即可，不必过度投入

---

## 六、Web 页面使用

浏览器访问：`http://<服务器IP>:8000/ui`

- **语音合成**：输入文本 → 选择"使用已保存音色"（下拉选择）或"临时上传音频" → 点击"开始合成" → 查看音频与各阶段耗时。
- **音色管理**：上传音频（可勾选"上传后立即提取并保存特征"）→ 在列表中查看音色 ID、时长、是否已提取特征 → 支持删除与重新提取。

## 七、接口说明

> **完整接口文档**（Swagger 风格，含全部参数、响应字段、示例与错误码）：[`server/API.md`](API.md)

交互式接口文档（Swagger UI）：`http://<服务器IP>:8000/docs`

### 1. 上传源音频并创建音色

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/voices" \
  -F "file=@./ref.mp3" \
  -F "name=我的音色" \
  -F "ref_text=" \
  -F "extract=true"
```

`ref_text` 留空时由 Whisper 自动识别参考音频内容；`extract=true` 表示上传后立即提取并保存特征。

### 2. 查看音色列表

```bash
curl "http://127.0.0.1:8000/api/v1/voices"
```

### 3. 使用已保存音色合成（直接复用特征）

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=你好，这是一段克隆语音测试。" \
  -F "voice_id=voice_20260903120000_ab12cd" \
  -o output.wav
```

### 4. 临时上传音频合成

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=你好，这是一段克隆语音测试。" \
  -F "file=@./ref.m4a" \
  -o output.wav
```

需要同时把这段音频留存为音色时，追加 `-F "save_as_voice=true" -F "voice_name=新音色"`。

### 5. 返回 JSON（含各阶段耗时）

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=你好" \
  -F "voice_id=voice_20260903120000_ab12cd" \
  -F "response_format=json"
```

返回示例：

```json
{
  "请求编号": "tts-1a2b3c4d",
  "提示": "合成成功",
  "音频格式": "wav",
  "采样率": 24000,
  "音频时长秒": 3.42,
  "音频Base64": "...",
  "使用的音色ID": "voice_20260903120000_ab12cd",
  "是否复用已保存特征": true,
  "阶段耗时": [
    { "阶段": "请求参数校验", "耗时毫秒": 0.3, "备注": "" },
    { "阶段": "加载已保存特征", "耗时毫秒": 12.5, "备注": "" },
    { "阶段": "语音合成", "耗时毫秒": 1980.4, "备注": "" },
    { "阶段": "音频编码与写出", "耗时毫秒": 8.1, "备注": "" }
  ],
  "总耗时毫秒": 2001.3
}
```

直接返回 wav 时，耗时信息同样放在响应头中：`X-Request-Id`、`X-Total-Time-Ms`、`X-Audio-Duration-Sec`、`X-Voice-Id`、`X-Feature-Reused`。

### 6. JSON + base64 调用（无文件上传能力时使用）

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts/base64" \
  -H "Content-Type: application/json" \
  -d '{"text":"你好","audio_base64":"<base64内容>","audio_format":"mp3","response_format":"json"}'
```

### 7. 其他接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/info` | 服务信息（模型、设备、存储统计、上传限制） |
| GET | `/api/v1/voices/{音色ID}` | 音色详情 |
| PATCH | `/api/v1/voices/{音色ID}` | 修改音色名称 / 参考文本 |
| DELETE | `/api/v1/voices/{音色ID}` | 删除音色（含源音频与特征） |
| POST | `/api/v1/voices/{音色ID}/extract` | 提取或重新提取特征 |
| GET | `/api/v1/voices/{音色ID}/audio` | 下载源音频 |
| GET | `/api/v1/outputs` | 合成结果列表 |
| GET | `/api/v1/outputs/{文件名}` | 下载合成结果 |

---

## 八、日志与耗时

全流程中文日志，同时输出到控制台（`docker logs -f omnivoice`）与 `data/logs/service.log`。示例：

```
2026-09-03 10:12:01 | 信息 | 服务 | ============== OmniVoice 语音克隆服务启动中，版本 1.0.0 ==============
2026-09-03 10:12:01 | 信息 | 服务 | 配置摘要：服务监听地址=0.0.0.0:8000；模型=k2-fsa/OmniVoice；...
2026-09-03 10:12:03 | 信息 | 服务 | [tts-1a2b3c4d] 阶段【加载已保存特征】开始 ...
2026-09-03 10:12:03 | 信息 | 语音库 | 特征缓存命中（内存），音色ID=voice_20260903120000_ab12cd
2026-09-03 10:12:03 | 信息 | 服务 | [tts-1a2b3c4d] 阶段【加载已保存特征】结束，耗时 12.5 毫秒
2026-09-03 10:12:05 | 信息 | 引擎 | 语音合成完成，耗时 1980.4 毫秒，文本长度=15 字，音频时长=3.42 秒
2026-09-03 10:12:05 | 信息 | 服务 | [tts-1a2b3c4d] 语音合成全部完成，总耗时 2001.3 毫秒；阶段明细：请求参数校验=0.3毫秒、加载已保存特征=12.5毫秒、语音合成=1980.4毫秒、音频编码与写出=8.1毫秒
```

通过阶段明细即可直观看出耗时主要落在"语音合成"还是"特征提取"，从而判断应该优化步数、缩短参考音频，还是提升显卡性能。

---

## 九、性能测试（压测）

### 9.1 并发模型（压测前必读）

服务支持**按显存余量自适应的并发推理**：

- 每次推理前申请一个"槽位"：并发数未达上限**且**显存充足时立即放行；否则进入等待队列
- 有推理完成后释放槽位，等待中的请求按序补位
- 单次推理的显存占用会**自适应学习**（也可手动指定），避免估算不准

**第一层：显存硬约束（防 OOM）**

```
可分配显存容量 − GPU_RESERVE_MB ≥ 单次推理估算显存    且    当前推理数 < MAX_CONCURRENCY
```

其中**可分配显存容量**不是简单的设备剩余，而是：

```
可分配容量 = 缓存池空闲(memory_reserved − memory_allocated) + 设备级剩余(free)
```

> 为什么要这样算：PyTorch 的缓存分配器在张量释放后会把显存留在进程缓存池里，**不归还驱动**。
> 因此"设备级剩余显存"会明显低于实际可用量，出现"显存被占用、但 GPU 其实很闲"的情况。
> 只看设备剩余会误判为显存不足而拒绝放行，把缓存池空闲计入即可避免这种误拒。

**第二层：利用率软约束（防无效并发，可选）**

设置 `GPU_UTILIZATION_LIMIT`（如 `90`）后，当**已有推理在跑**且 GPU 利用率达到该值时，
即使显存够也暂不放行，避免并发只增加排队、不提升吞吐。

- 需要容器内安装 `pynvml`（`pip install nvidia-ml-py`）才生效；没装则自动跳过该约束
- 首个请求不受此约束限制——否则显卡被其他进程占满时，本服务会一个请求都跑不了
- 默认 `0`（关闭），仅在需要精细控制并发时开启

- GPU 环境：默认并发上限 2（显存充足时；T4 建议 2~3，A10/A100 可更高）
- CPU 环境：无显存概念，默认并发上限 1（并发会争抢 CPU，反而可能变慢）

理论吞吐上限：

```
QPS上限 ≈ MAX_CONCURRENCY / 单次合成耗时（秒）
```

例：并发 2、单条耗时 2 秒，则 QPS 上限约 1.0。超过承载后，继续加压只会增加排队时间。

**排队情况可观测**：并发时若请求等待了槽位，接口返回的阶段耗时中会出现 `等待推理槽位` 阶段，日志中也会打印"请求等待推理槽位 X 毫秒后进入推理"。

### 9.2 压测工具

项目自带压测脚本 `server/tools/benchmark.py`，需先安装依赖：

```bash
pip install httpx
```

**常用命令**：

```bash
cd server/tools

# 复用已保存音色（生产主要路径，推荐）
python benchmark.py --url http://127.0.0.1:8000 \
  --voice-id voice_xxx --concurrency 1 2 4 8 --requests 20

# 自动创建音色后压测（没有现成音色时）
python benchmark.py --url http://127.0.0.1:8000 --audio ./ref.wav \
  --auto-create --concurrency 4 --requests 30

# 临时上传音频（每次重新提取特征，最坏情况）
python benchmark.py --url http://127.0.0.1:8000 --audio ./ref.wav \
  --mode upload --concurrency 1 2 --requests 10

# 结果保存为 JSON
python benchmark.py --url http://127.0.0.1:8000 --concurrency 2 4 \
  --requests 20 --output result.json
```

**参数说明**：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--url` | `http://127.0.0.1:8000` | 服务地址 |
| `--voice-id` | 空 | 已保存音色 ID；为空时自动取列表中的第一个 |
| `--audio` | 空 | 参考音频路径 |
| `--auto-create` | 关闭 | 用 `--audio` 创建音色后再压测 |
| `--mode` | `reuse` | `reuse` 复用音色 / `upload` 临时上传 |
| `--text` | 内置文本 | 待合成文本，建议换成实际业务长度 |
| `--concurrency` | `1 2 4` | 并发梯度，可指定多轮依次测试 |
| `--requests` | `20` | 每轮请求总数 |
| `--timeout` | `600` | 单请求超时（秒），CPU 模式请调大 |
| `--warmup` | `1` | 预热次数，不计入统计 |
| `--output` | 空 | 结果 JSON 保存路径 |

### 9.3 输出指标解读

脚本输出示例：

```
并发   1 | 请求  20 | 成功  20 | 失败   0 | 成功率 100.0% | QPS  0.512
        客户端耗时：平均   1953.2 ms | P50   1940.1 | P95   2100.5 | P99   2150.0 | 最大   2160.3
        服务端耗时：平均   1920.4 ms | P95   2050.0    排队耗时：平均     32.8 ms | 最大     60.1
```

| 指标 | 含义 | 用途 |
| --- | --- | --- |
| `QPS` | 每秒完成请求数（按墙钟时间计算） | 吞吐能力，受并发上限与显存余量限制 |
| `客户端平均 / P50 / P95 / P99` | 客户端观测到的响应耗时分布 | 评估用户实际体验 |
| `服务端耗时` | 取自响应头 `X-Total-Time-Ms`，服务端真实处理时间 | 评估模型推理本身 |
| `排队耗时` | 客户端耗时 − 服务端耗时 | 反映等待推理锁的时间，越大说明并发已超出处理能力 |

汇总表会按并发梯度列出对比，可直观看到拐点。

### 9.4 压测步骤建议

1. **在服务端同机或同局域网测试**，避免网络延迟干扰结果
2. **先测并发 1**，得到单次合成耗时基线，据此推算 QPS 上限
3. **逐步加大并发**（1→2→4→8），观察"排队耗时"何时显著增长，该拐点即服务的实际承载并发
4. **同时观察资源**：`docker stats` 查看 CPU / 内存 / 显存占用，判断瓶颈在算力还是排队
5. **使用真实业务文本长度**，文本越长耗时越高

### 9.5 提升吞吐的办法

| 办法 | 说明 |
| --- | --- |
| 降低 `DEFAULT_NUM_STEP` | 单次耗时直接下降，QPS 同比提升（效果最明显） |
| 提高 `MAX_CONCURRENCY` | 显存有余量时放宽并发上限，吞吐随之提升 |
| 多容器 + 多显卡 | 每个容器绑定不同 GPU（`--gpus '"device=1"'`），吞吐近似线性提升 |
| 缩短参考音频 | 减少特征提取耗时（仅影响首次上传） |
| 批量合成 | 批量场景吞吐更高，当前接口为单条，可按需扩展 |

> 并发受 `MAX_CONCURRENCY` 与显存余量双重限制。达到上限后继续加压不会提升吞吐，只会增加排队。需要更高吞吐，请降低单次耗时、在显存允许时提高并发上限，或部署多容器多卡。

## 十、运行机制说明

- **按显存自适应的并发推理**：服务为单进程，推理并发由"槽位"控制——显存充足即放行，不足则排队等待（详见 9.1）。默认 GPU 并发上限 2、CPU 为 1，可用 `MAX_CONCURRENCY` 调整；如需更高吞吐，可部署多个容器并分别绑定不同显卡（`--gpus '"device=1"'`）。
- **特征复用流程**：源音频首次上传时转码 + 提取特征并落盘；之后每次合成只加载 `prompt.pt`，不再重复做静音裁剪、音频编码与 ASR 识别，因此第二次起耗时会明显下降（日志中体现为"加载已保存特征"而非"声纹特征提取"）。
- **结果文件累积**：开启 `SAVE_OUTPUT` 后，合成结果会持续写入 `data/outputs/`，长期运行建议定期清理或挂载较大磁盘；设置 `SAVE_OUTPUT=false` 可只返回音频、不落盘。
- **可选参数请勿传空值**：`num_step`、`speed`、`duration` 等可选参数不传即可，不要传空字符串（例如 `-F "speed="`），否则接口会返回 422 参数校验错误。

## 十一、常见问题

**1. 首次启动很慢？**
首次需要下载模型（约数 GB）。请把 `/opt/models` 挂载到宿主机，后续重建容器无需重新下载。

**2. HuggingFace 下载失败？**
启动时增加 `-e HF_ENDPOINT=https://hf-mirror.com`。

**3. mp3 / m4a 上传后提示无法解码？**
确认容器内 ffmpeg 可用：`docker exec omnivoice ffmpeg -version`。镜像已内置 ffmpeg；若使用自定义镜像请自行安装。

**4. Docker 18.09 启动报 `--gpus` 未知参数？**
请改用 `--runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=0`（需先安装 nvidia-docker2）。

**5. 显存溢出（CUDA out of memory）？**
降低 `num_step`（如 16）、缩短参考音频（`-e MAX_REF_DURATION=15`）、或换用更大显存的显卡。

**6. 不想加载 Web 页面，只保留接口？**
启动时增加 `-e ENABLE_WEBUI=false`。
