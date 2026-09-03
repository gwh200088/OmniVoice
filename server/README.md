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

## 三、环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 服务监听地址与端口 |
| `UI_PATH` / `API_PREFIX` | `/ui` / `/api/v1` | Web 页面路径、接口前缀 |
| `MODEL_ID` | `k2-fsa/OmniVoice` | 模型标识或本地路径 |
| `DEVICE` | 空（自动检测） | 如 `cuda:0`、`cpu` |
| `DTYPE` | `float16` | `float16` / `bfloat16` / `float32`（CPU 自动转 float32） |
| `ATTN_IMPLEMENTATION` | 空（自动） | 注意力实现，如 `sdpa` |
| `PRELOAD_MODEL` | `true` | 启动时预加载模型（首次请求不再等待） |
| `LOAD_ASR` / `ASR_MODEL` | `true` / `openai/whisper-large-v3-turbo` | 是否加载 ASR 用于自动识别参考文本 |
| `ASR_DEVICE` | 空 | ASR 模型所在设备，如 `cuda:0` / `cpu` |
| `MAX_UPLOAD_MB` | `50` | 单个音频大小上限 |
| `MAX_REF_DURATION` | `30` | 参考音频最长保留秒数（超出自动截断） |
| `FEATURE_CACHE_SIZE` | `64` | 特征内存缓存条数（LRU） |
| `SAVE_OUTPUT` | `true` | 是否保存合成结果到 `data/outputs` |
| `ENABLE_WEBUI` | `true` | 是否启用 Web 页面 |
| `LOG_LEVEL` / `LOG_TO_FILE` | `INFO` / `true` | 日志级别、是否写日志文件 |
| `DATA_DIR` | `/opt/omnivoice-service/data` | 数据根目录 |
| `HF_ENDPOINT` | 空 | HuggingFace 镜像，如 `https://hf-mirror.com` |

## 四、不同显卡环境建议

| 显卡 | 建议参数 | 说明 |
| --- | --- | --- |
| T4（16GB，sm_75） | `DTYPE=float16` | 不支持 flash-attn，镜像未安装，自动使用 SDPA |
| A10（24GB，sm_86） | `DTYPE=float16` 或 `bfloat16` | 显存更充裕，可适当调高 `FEATURE_CACHE_SIZE` |
| A100 / H100 | `DTYPE=bfloat16` | 精度更好 |
| 无显卡（CPU 调试） | `DEVICE=cpu` `DTYPE=float32` | 可跑通流程，但速度很慢 |

显存紧张时可降低 `num_step`（如 16）、缩短参考音频（`MAX_REF_DURATION=15`）。

---

## 五、Web 页面使用

浏览器访问：`http://<服务器IP>:8000/ui`

- **语音合成**：输入文本 → 选择"使用已保存音色"（下拉选择）或"临时上传音频" → 点击"开始合成" → 查看音频与各阶段耗时。
- **音色管理**：上传音频（可勾选"上传后立即提取并保存特征"）→ 在列表中查看音色 ID、时长、是否已提取特征 → 支持删除与重新提取。

## 六、接口说明

接口文档（Swagger）：`http://<服务器IP>:8000/docs`

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

## 七、日志与耗时

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

## 八、运行机制说明

- **单进程串行推理**：服务默认单进程运行，推理过程加锁串行执行。并发请求会自动排队，不会因抢显存导致 OOM；如需更高吞吐，可部署多个容器并分别绑定不同显卡（`--gpus '"device=1"'`）。
- **特征复用流程**：源音频首次上传时转码 + 提取特征并落盘；之后每次合成只加载 `prompt.pt`，不再重复做静音裁剪、音频编码与 ASR 识别，因此第二次起耗时会明显下降（日志中体现为"加载已保存特征"而非"声纹特征提取"）。
- **结果文件累积**：开启 `SAVE_OUTPUT` 后，合成结果会持续写入 `data/outputs/`，长期运行建议定期清理或挂载较大磁盘；设置 `SAVE_OUTPUT=false` 可只返回音频、不落盘。
- **可选参数请勿传空值**：`num_step`、`speed`、`duration` 等可选参数不传即可，不要传空字符串（例如 `-F "speed="`），否则接口会返回 422 参数校验错误。

## 九、常见问题

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
