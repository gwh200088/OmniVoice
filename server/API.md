# OmniVoice 语音克隆服务 · 接口说明文档

本文档以 Swagger 风格逐接口说明服务提供的 HTTP API，包含请求参数、响应字段、示例与错误码。

> **在线交互文档**：服务启动后自带 Swagger UI，可直接在线调试：
> - Swagger UI：`http://<服务地址>:8000/docs`
> - OpenAPI 规范（JSON）：`http://<服务地址>:8000/openapi.json`
> - Web 操作页面：`http://<服务地址>:8000/ui`

---

## 1. 通用约定

| 项目 | 说明 |
| --- | --- |
| 协议 | HTTP / HTTPS |
| 基础路径 | `http://<服务地址>:<端口>`（默认端口 `8000`） |
| 接口前缀 | `/api/v1`（可通过环境变量 `API_PREFIX` 修改） |
| 认证方式 | 无（如需鉴权请在反向代理层实现） |
| 请求编码 | 文件上传用 `multipart/form-data`；JSON 接口用 `application/json` |
| 响应编码 | JSON 接口为 `application/json`；音频接口为 `audio/wav` |
| 字符集 | UTF-8 |

### 1.1 统一响应结构

所有 JSON 响应均使用**中文键名**，含义直观，便于排查问题。

**成功响应**（以创建音色为例）：

```json
{
  "请求编号": "voice-1a2b3c4d",
  "提示": "音色创建成功",
  "音色信息": { "...": "音色对象，见 2.1" },
  "阶段耗时": [ { "...": "阶段耗时对象，见 2.2" } ],
  "总耗时毫秒": 3861.7
}
```

**错误响应**：

```json
{
  "detail": "请求参数错误：待合成的文本不能为空。"
}
```

### 1.2 状态码

| 状态码 | 含义 | 常见场景 |
| --- | --- | --- |
| 200 | 成功 | 请求正常处理 |
| 400 | 请求参数错误 | 文本为空、未提供音色来源、音频格式不支持、文件超限 |
| 404 | 资源不存在 | 音色 ID 不存在、文件不存在 |
| 422 | 参数校验失败 | 可选数值参数传了空字符串、类型不匹配 |
| 500 | 服务内部错误 | 模型加载失败、推理异常、磁盘写入失败 |

### 1.3 耗时响应头

语音合成接口（`POST /tts`、`POST /tts/base64`）无论返回音频还是 JSON，都会携带以下响应头，方便在不解析响应体的情况下获取耗时：

| 响应头 | 说明 | 示例 |
| --- | --- | --- |
| `X-Request-Id` | 请求编号，与日志中的编号一致，可用于串联日志 | `tts-1a2b3c4d` |
| `X-Total-Time-Ms` | 全流程总耗时（毫秒） | `2001.3` |
| `X-Audio-Duration-Sec` | 生成音频的时长（秒） | `3.42` |
| `X-Voice-Id` | 使用的音色 ID；临时上传时为 `-` | `voice_20260903_abc123` |
| `X-Feature-Reused` | 是否复用了已保存的特征（`true` / `false`） | `true` |

---

## 2. 数据模型

### 2.1 音色对象（VoiceMeta）

音色列表、详情、创建、修改等接口的响应中都会包含该对象。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `音色ID` | string | 音色唯一标识，形如 `voice_20260903120000_ab12cd`，用于合成、删除、重新提取 |
| `音色名称` | string | 音色名称，创建时未指定则取文件名 |
| `创建时间` | string | 格式 `YYYY-MM-DD HH:MM:SS` |
| `更新时间` | string | 最后一次修改信息或重新提取特征的时间 |
| `原始文件名` | string | 上传时的文件名 |
| `原始格式` | string | 上传音频的扩展名，如 `mp3`、`m4a` |
| `原始文件大小字节` | integer | 上传文件的大小 |
| `文件MD5` | string | 源文件 MD5，用于识别重复上传 |
| `原始音频时长秒` | number | 上传音频的原始时长 |
| `音频时长秒` | number | 规范化后音频的时长（单声道、24 kHz） |
| `采样率` | integer | 规范化后音频采样率，通常 24000 |
| `声道数` | integer | 规范化后音频声道数，固定为 1 |
| `参考文本` | string | 参考音频对应的文本；为空表示尚未识别 |
| `参考文本来源` | string | `用户填写` / `ASR 自动识别` / `待识别` |
| `是否已提取特征` | boolean | 声纹特征是否已提取并落盘 |
| `特征提取耗时毫秒` | number | 上次特征提取的耗时 |
| `特征提取时间` | string | 上次提取特征的时间，未提取时为空 |
| `特征文件大小字节` | integer | 特征文件（`prompt.pt`）大小 |
| `特征文件路径` | string | 容器内特征文件绝对路径 |
| `源音频文件路径` | string | 容器内规范化音频绝对路径 |

### 2.2 阶段耗时对象（StageTiming）

服务对全流程分段计时，便于定位性能瓶颈。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `阶段` | string | 阶段名称，见下表 |
| `耗时毫秒` | number | 该阶段耗时 |
| `备注` | string | 补充说明，无则为空字符串 |

**阶段名称与含义**：

| 阶段 | 出现场景 | 说明 |
| --- | --- | --- |
| `请求参数校验` | 所有接口 | 参数合法性检查（格式、大小、音色来源） |
| `音频解码与转码` | 上传音频时 | ffmpeg 解码并统一转码为单声道 24 kHz wav |
| `声纹特征提取` | 首次使用某段音频 | 音频编码 + ASR 识别参考文本（**最耗时，通常数百毫秒到数秒**） |
| `加载已保存特征` | 复用已有音色 | 从内存缓存或磁盘加载特征（**复用时不再出现"声纹特征提取"**） |
| `特征写入磁盘` | 提取特征后 | 把特征保存为 `prompt.pt` |
| `语音合成` | 所有合成请求 | 扩散解码生成音频（**长文本的主要耗时来源**） |
| `等待推理槽位` | 并发较高时 | 排队等待显存或并发槽位释放，**并发场景排障的关键指标** |
| `音频编码与写出` | 所有合成请求 | 编码为 wav 并按需落盘 |

### 2.3 错误对象

```json
{ "detail": "错误描述（中文）" }
```

---

## 3. 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务首页（入口信息） |
| GET | `/api/v1/health` | 健康检查 |
| GET | `/api/v1/info` | 服务信息（模型、设备、存储、限制） |
| GET | `/api/v1/voices` | 音色列表 |
| POST | `/api/v1/voices` | 上传源音频并创建音色 |
| GET | `/api/v1/voices/{voice_id}` | 音色详情 |
| PATCH | `/api/v1/voices/{voice_id}` | 修改音色信息 |
| DELETE | `/api/v1/voices/{voice_id}` | 删除音色 |
| POST | `/api/v1/voices/{voice_id}/extract` | 提取 / 重新提取特征 |
| GET | `/api/v1/voices/{voice_id}/audio` | 下载源音频 |
| POST | `/api/v1/tts` | 语音合成（文件上传） |
| POST | `/api/v1/tts/base64` | 语音合成（JSON + base64） |
| GET | `/api/v1/outputs` | 合成结果列表 |
| GET | `/api/v1/outputs/{filename}` | 下载合成结果 |

---

## 4. 接口详情

### 4.1 服务首页

```
GET /
```

返回服务基本信息与常用入口，适合作为存活探活。

**请求参数**：无

**响应示例**：

```json
{
  "服务名称": "OmniVoice 语音克隆服务",
  "版本": "1.0.0",
  "Web 页面": "/ui",
  "接口文档": "/docs",
  "健康检查": "/api/v1/health",
  "服务信息": "/api/v1/info",
  "说明": "模型若未预加载，首次请求时会自动加载，可能需要等待数十秒。"
}
```

---

### 4.2 健康检查

```
GET /api/v1/health
```

用于容器健康检查与负载均衡探活，**不加载模型**，返回极快。

**请求参数**：无

**响应示例**：

```json
{
  "状态": "正常",
  "模型已加载": true,
  "推理设备": "cuda",
  "数据目录": "/opt/omnivoice-service/data"
}
```

---

### 4.3 服务信息

```
GET /api/v1/info
```

返回模型、设备、存储统计与运行限制。

**请求参数**：无

**响应示例**：

```json
{
  "服务版本": "1.0.0",
  "引擎信息": {
    "模型": "k2-fsa/OmniVoice",
    "模型已加载": true,
    "推理设备": "cuda:0",
    "显卡名称": "Tesla T4",
    "计算精度": "torch.float16",
    "采样率": 24000,
    "模型加载耗时毫秒": 12450.3,
    "并发槽位": {
      "最大并发数": 2,
      "当前推理数": 1,
      "等待队列长度": 0,
      "历史最大并发": 2,
      "单次推理显存估算(MB)": 2048.0,
      "显存估算方式": "自适应学习",
      "显存安全预留(MB)": 512.0,
      "可分配显存容量(MB)": 12288.0,
      "设备级剩余显存(MB)": 6144.0,
      "进程已分配(MB)": 4096.0,
      "进程已预留(MB)": 10240.0,
      "缓存池空闲(MB)": 6144.0,
      "显存总量(MB)": 16384.0,
      "GPU利用率(%)": 68.0,
      "利用率上限(%)": 0.0
    }
  },
  "存储统计": {
    "音色总数": 5,
    "已提取特征数": 5,
    "内存缓存条数": 3,
    "特征占用字节": 6291456,
    "存储根目录": "/opt/omnivoice-service/data/voices"
  },
  "上传限制": {
    "单个文件最大MB": 50,
    "参考音频最大秒数": 30,
    "支持的音频格式": ["wav", "mp3", "m4a", "flac", "ogg", "aac"]
  },
  "合成默认参数": {
    "扩散步数": 32,
    "引导系数": 2.0,
    "采样率": 24000
  }
}
```

---

### 4.4 音色列表

```
GET /api/v1/voices
```

按创建时间倒序列出所有已保存的音色。

**请求参数**：无

**响应示例**：

```json
{
  "总数": 2,
  "音色列表": [
    {
      "音色ID": "voice_20260903120000_ab12cd",
      "音色名称": "客服小美",
      "创建时间": "2026-09-03 12:00:00",
      "音频时长秒": 6.5,
      "是否已提取特征": true,
      "参考文本来源": "ASR 自动识别",
      "...": "其余字段见 2.1"
    }
  ]
}
```

---

### 4.5 上传源音频并创建音色

```
POST /api/v1/voices
Content-Type: multipart/form-data
```

上传一段参考音频，登记为音色并可选立即提取声纹特征。提取完成后特征落盘，后续合成直接复用。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `file` | formData | file | 是 | - | 音频文件，支持 wav / mp3 / m4a / flac / ogg / aac 等 |
| `name` | formData | string | 否 | 空（使用文件名） | 音色名称 |
| `ref_text` | formData | string | 否 | 空 | 参考音频对应的文本；留空则由 ASR 自动识别 |
| `extract` | formData | boolean | 否 | `true` | 是否上传后立即提取并保存特征 |

**请求示例**：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/voices" \
  -F "file=@./ref.mp3" \
  -F "name=客服小美" \
  -F "ref_text=" \
  -F "extract=true"
```

**响应示例**：

```json
{
  "请求编号": "voice-30fd95e4",
  "提示": "音色创建成功",
  "音色信息": {
    "音色ID": "voice_20260903120000_ab12cd",
    "音色名称": "客服小美",
    "音频时长秒": 6.5,
    "是否已提取特征": true,
    "特征提取耗时毫秒": 892.4,
    "参考文本来源": "ASR 自动识别",
    "...": "其余字段见 2.1"
  },
  "阶段耗时": [
    { "阶段": "请求参数校验", "耗时毫秒": 0.1, "备注": "" },
    { "阶段": "音频解码与转码", "耗时毫秒": 210.5, "备注": "" },
    { "阶段": "声纹特征提取", "耗时毫秒": 892.4, "备注": "" },
    { "阶段": "特征写入磁盘", "耗时毫秒": 12.8, "备注": "" }
  ],
  "总耗时毫秒": 1116.2
}
```

**错误码**：`400`（文件为空 / 格式不支持 / 超过大小上限）、`500`（转码或提取失败）

---

### 4.6 音色详情

```
GET /api/v1/voices/{voice_id}
```

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `voice_id` | path | string | 是 | 音色 ID |

**响应示例**：

```json
{
  "音色信息": { "音色ID": "voice_20260903120000_ab12cd", "...": "见 2.1" }
}
```

**错误码**：`404`（音色不存在）

---

### 4.7 修改音色信息

```
PATCH /api/v1/voices/{voice_id}
Content-Type: application/json
```

用于修改音色名称或补充/修正参考文本。**未传的字段保持不变**。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `voice_id` | path | string | 是 | 音色 ID |
| `name` | body | string | 否 | 新的音色名称 |
| `ref_text` | body | string | 否 | 参考音频对应的文本 |

**请求示例**：

```bash
curl -X PATCH "http://127.0.0.1:8000/api/v1/voices/voice_20260903120000_ab12cd" \
  -H "Content-Type: application/json" \
  -d '{"name": "客服小美-正式版", "ref_text": "大家好，我是客服小美。"}'
```

**响应示例**：

```json
{
  "提示": "音色信息已更新",
  "音色信息": { "音色名称": "客服小美-正式版", "...": "见 2.1" }
}
```

**错误码**：`404`（音色不存在）

> 说明：修改 `ref_text` 只会更新元数据，**不会自动重新提取特征**。如需让新文本生效，请调用 `/extract` 重新提取。

---

### 4.8 删除音色

```
DELETE /api/v1/voices/{voice_id}
```

删除音色的源音频、特征文件与元数据，同时清理内存缓存。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `voice_id` | path | string | 是 | 音色 ID |

**请求示例**：

```bash
curl -X DELETE "http://127.0.0.1:8000/api/v1/voices/voice_20260903120000_ab12cd"
```

**响应示例**：

```json
{
  "提示": "音色已删除",
  "音色ID": "voice_20260903120000_ab12cd"
}
```

**错误码**：`404`（音色不存在）

---

### 4.9 提取 / 重新提取特征

```
POST /api/v1/voices/{voice_id}/extract
Content-Type: application/json
```

为已上传的源音频提取声纹特征并保存。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `voice_id` | path | string | 是 | - | 音色 ID |
| `ref_text` | body | string | 否 | null | 参考文本；传 null 或不传则沿用手写文本 / ASR 自动识别 |
| `force` | body | boolean | 否 | `true` | 为 `true` 时即使已有特征也重新计算 |

**请求示例**：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/voices/voice_20260903120000_ab12cd/extract" \
  -H "Content-Type: application/json" \
  -d '{"ref_text": "大家好，我是客服小美。", "force": true}'
```

**响应示例（执行提取）**：

```json
{
  "请求编号": "reextract-7f8a9b0c",
  "提示": "特征提取完成",
  "音色信息": { "是否已提取特征": true, "特征提取耗时毫秒": 910.2, "...": "见 2.1" },
  "阶段耗时": [ { "阶段": "声纹特征提取", "耗时毫秒": 910.2, "备注": "" } ],
  "总耗时毫秒": 925.7
}
```

**响应示例（已存在特征且 `force=false`）**：

```json
{
  "提示": "该音色已存在特征，未重复提取（如需强制重算请设置 force=true）",
  "音色信息": { "...": "见 2.1" }
}
```

> 注意：该场景下响应**不包含** `请求编号`、`阶段耗时`、`总耗时毫秒` 三个字段。

**错误码**：`404`（音色不存在）

---

### 4.10 下载源音频

```
GET /api/v1/voices/{voice_id}/audio
```

下载规范化后的源音频（单声道、24 kHz、16 bit wav）。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `voice_id` | path | string | 是 | 音色 ID |

**响应**：`audio/wav` 二进制流，文件名 `{voice_id}.wav`

**请求示例**：

```bash
curl -OJ "http://127.0.0.1:8000/api/v1/voices/voice_20260903120000_ab12cd/audio"
```

**错误码**：`404`（音色不存在或源文件缺失）

---

### 4.11 语音合成（文件上传）

```
POST /api/v1/tts
Content-Type: multipart/form-data
```

核心接口。音色来源**二选一**：已保存的音色 ID（推荐，直接复用特征）或本次临时上传的音频。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `text` | formData | string | **是** | - | 待合成的文本。支持 `[laughter]` 等非语言符号、拼音/音标纠正发音 |
| `voice_id` | formData | string | 二选一 | - | 已保存的音色 ID；与 `file` **互斥**，不可同时提供 |
| `file` | formData | file | 二选一 | - | 临时上传的参考音频；与 `voice_id` **互斥** |
| `ref_text` | formData | string | 否 | - | 参考音频文本，仅临时上传时有意义；留空自动识别 |
| `language` | formData | string | 否 | - | 语种，如 `Chinese`、`English`、`en`；留空自动判断 |
| `instruct` | formData | string | 否 | - | 声音设计描述，如 `female, low pitch, british accent` |
| `duration` | formData | number | 否 | - | 固定输出时长（秒），设置后忽略 `speed` |
| `speed` | formData | number | 否 | - | 语速倍率，>1 更快，<1 更慢 |
| `num_step` | formData | integer | 否 | `32` | 扩散步数；调低（如 16）可显著提速 |
| `guidance_scale` | formData | number | 否 | `2.0` | CFG 引导系数 |
| `denoise` | formData | boolean | 否 | `true` | 是否启用降噪 |
| `preprocess_prompt` | formData | boolean | 否 | `true` | 参考音频预处理（静音裁剪等） |
| `postprocess_output` | formData | boolean | 否 | `true` | 输出音频后处理（去除长静音等） |
| `save_as_voice` | formData | boolean | 否 | `false` | 是否把临时上传的音频另存为音色 |
| `voice_name` | formData | string | 否 | 空 | `save_as_voice=true` 时的音色名称 |
| `response_format` | formData | string | 否 | `wav` | `wav` 返回音频二进制；`json` 返回 base64 与耗时明细 |

**请求示例 1：使用已保存音色（推荐）**

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=您好，这里是智能客服，请问有什么可以帮您？" \
  -F "voice_id=voice_20260903120000_ab12cd" \
  -o output.wav
```

**请求示例 2：临时上传音频（用完即弃）**

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=您好，这里是智能客服。" \
  -F "file=@./ref.m4a" \
  -o output.wav
```

**请求示例 3：临时上传并另存为音色**

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=您好，这里是智能客服。" \
  -F "file=@./ref.m4a" \
  -F "save_as_voice=true" \
  -F "voice_name=客服小美" \
  -F "response_format=json"
```

**请求示例 4：返回 JSON（含各阶段耗时）**

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=您好，这里是智能客服。" \
  -F "voice_id=voice_20260903120000_ab12cd" \
  -F "num_step=16" \
  -F "response_format=json"
```

**响应（`response_format=wav`，默认）**：

直接返回 `audio/wav` 二进制，响应头见 1.3。

**响应（`response_format=json`）**：

```json
{
  "请求编号": "tts-1a2b3c4d",
  "提示": "合成成功",
  "音频格式": "wav",
  "采样率": 24000,
  "音频时长秒": 3.42,
  "音频Base64": "UklGRiQAAABXQVZFZm10IBAAAAABAAEA...",
  "使用的音色ID": "voice_20260903120000_ab12cd",
  "是否复用已保存特征": true,
  "输出文件路径": "/opt/omnivoice-service/data/outputs/20260903_tts-1a2b3c4d.wav",
  "阶段耗时": [
    { "阶段": "请求参数校验", "耗时毫秒": 0.3, "备注": "" },
    { "阶段": "加载已保存特征", "耗时毫秒": 12.5, "备注": "" },
    { "阶段": "语音合成", "耗时毫秒": 1980.4, "备注": "" },
    { "阶段": "音频编码与写出", "耗时毫秒": 8.1, "备注": "" }
  ],
  "总耗时毫秒": 2001.3
}
```

**错误码**：`400`（文本为空 / 未指定音色来源 / 同时提供两者 / 音频格式不支持）、`404`（音色不存在）、`500`（推理失败）

---

### 4.12 语音合成（JSON + base64）

```
POST /api/v1/tts/base64
Content-Type: application/json
```

功能与 `/tts` 完全一致，仅传参方式不同，适合无法使用 `multipart/form-data` 的客户端。

**请求参数**：

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `text` | string | **是** | - | 待合成的文本 |
| `voice_id` | string | 二选一 | null | 已保存的音色 ID |
| `audio_base64` | string | 二选一 | null | 临时上传音频的 base64 内容 |
| `audio_format` | string | 否 | `wav` | 临时音频的格式，如 `wav` / `mp3` / `m4a` |
| `ref_text` | string | 否 | null | 参考音频文本 |
| `language` | string | 否 | null | 语种 |
| `instruct` | string | 否 | null | 声音设计描述 |
| `duration` | number | 否 | null | 固定输出时长（秒） |
| `speed` | number | 否 | null | 语速倍率 |
| `num_step` | integer | 否 | null | 扩散步数 |
| `guidance_scale` | number | 否 | null | CFG 引导系数 |
| `denoise` | boolean | 否 | `true` | 是否降噪 |
| `preprocess_prompt` | boolean | 否 | `true` | 参考音频预处理 |
| `postprocess_output` | boolean | 否 | `true` | 输出后处理 |
| `save_as_voice` | boolean | 否 | `false` | 是否另存为音色 |
| `voice_name` | string | 否 | `""` | 另存时的音色名称 |
| `response_format` | string | 否 | `wav` | `wav` / `json` |

**请求示例（使用已保存音色）**：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/tts/base64" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "您好，这里是智能客服。",
        "voice_id": "voice_20260903120000_ab12cd",
        "response_format": "json"
      }'
```

**请求示例（临时上传音频）**：

```bash
AUDIO=$(base64 -w 0 ./ref.mp3)
curl -X POST "http://127.0.0.1:8000/api/v1/tts/base64" \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"您好，这里是智能客服。\",\"audio_base64\":\"$AUDIO\",\"audio_format\":\"mp3\",\"response_format\":\"json\"}"
```

**响应**：与 `/tts` 相同。

**错误码**：`400`、`404`、`422`、`500`

---

### 4.13 合成结果列表

```
GET /api/v1/outputs
```

列出已保存到本地的合成音频（需开启 `SAVE_OUTPUT`）。

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `limit` | query | integer | 否 | `50` | 返回条数上限，取值 1~500 |

**响应示例**：

```json
{
  "总数": 2,
  "文件列表": [
    {
      "文件名": "20260903_tts-1a2b3c4d.wav",
      "大小字节": 164244,
      "访问地址": "/outputs/20260903_tts-1a2b3c4d.wav"
    }
  ]
}
```

---

### 4.14 下载合成结果

```
GET /api/v1/outputs/{filename}
```

**请求参数**：

| 参数名 | 位置 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| `filename` | path | string | 是 | 文件名（含 `.wav`），取自列表接口的 `文件名` 字段 |

**响应**：`audio/wav` 二进制流

**请求示例**：

```bash
curl -OJ "http://127.0.0.1:8000/api/v1/outputs/20260903_tts-1a2b3c4d.wav"
```

**错误码**：`400`（文件名不合法）、`404`（文件不存在）

---

## 5. 调用示例

### 5.1 Python（requests）

```python
import requests

BASE = "http://127.0.0.1:8000/api/v1"

# 1) 上传源音频并创建音色（自动提取特征）
with open("ref.mp3", "rb") as f:
    resp = requests.post(
        f"{BASE}/voices",
        data={"name": "客服小美", "extract": "true"},
        files={"file": ("ref.mp3", f, "audio/mpeg")},
    )
resp.raise_for_status()
voice_id = resp.json()["音色信息"]["音色ID"]
print("音色 ID：", voice_id)

# 2) 使用该音色合成语音（复用特征，最快）
resp = requests.post(
    f"{BASE}/tts",
    data={
        "text": "您好，这里是智能客服，请问有什么可以帮您？",
        "voice_id": voice_id,
        "num_step": 16,
    },
)
resp.raise_for_status()
with open("out.wav", "wb") as f:
    f.write(resp.content)

print("总耗时（毫秒）：", resp.headers.get("X-Total-Time-Ms"))
print("是否复用特征：", resp.headers.get("X-Feature-Reused"))

# 3) 需要耗时明细时改用 JSON 返回
resp = requests.post(
    f"{BASE}/tts",
    data={"text": "您好，请问需要什么帮助？", "voice_id": voice_id, "response_format": "json"},
)
result = resp.json()
for stage in result["阶段耗时"]:
    print(f"{stage['阶段']}: {stage['耗时毫秒']} 毫秒")
```

### 5.2 JavaScript（浏览器 / Node.js）

```javascript
const BASE = "http://127.0.0.1:8000/api/v1";

// 上传音频创建音色
async function createVoice(file, name) {
  const form = new FormData();
  form.append("file", file);
  form.append("name", name);
  form.append("extract", "true");
  const resp = await fetch(`${BASE}/voices`, { method: "POST", body: form });
  if (!resp.ok) throw new Error((await resp.json()).detail);
  return (await resp.json())["音色信息"]["音色ID"];
}

// 合成语音并直接播放
async function synthesize(text, voiceId) {
  const form = new FormData();
  form.append("text", text);
  form.append("voice_id", voiceId);
  const resp = await fetch(`${BASE}/tts`, { method: "POST", body: form });
  if (!resp.ok) throw new Error((await resp.json()).detail);
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  new Audio(url).play();
  console.log("总耗时：", resp.headers.get("X-Total-Time-Ms"), "毫秒");
}
```

### 5.3 Java（OkHttp，上传 + 合成）

```java
OkHttpClient client = new OkHttpClient();

MultipartBody body = new MultipartBody.Builder()
        .setType(MultipartBody.FORM)
        .addFormDataPart("text", "您好，这里是智能客服。")
        .addFormDataPart("voice_id", "voice_20260903120000_ab12cd")
        .build();

Request request = new Request.Builder()
        .url("http://127.0.0.1:8000/api/v1/tts")
        .post(body)
        .build();

try (Response response = client.newCall(request).execute()) {
    if (!response.isSuccessful()) throw new IOException(response.body().string());
    System.out.println("总耗时：" + response.header("X-Total-Time-Ms") + " 毫秒");
    Files.write(Paths.get("out.wav"), response.body().bytes());
}
```

---

## 6. 典型业务流程

### 6.1 一次性使用（临时音频）

```
POST /api/v1/tts   (text + file)  →  得到音频
```

适合偶尔调用、不需要留存音色的场景。每次都会重新提取特征。

### 6.2 高频复用（推荐）

```
① POST /api/v1/voices          (上传音频，extract=true)  →  得到 voice_id
② POST /api/v1/tts             (text + voice_id)         →  得到音频（复用特征）
③ 重复 ②，无需再上传音频、无需重复提取
```

第一次提取特征后，后续每次合成只加载 `prompt.pt`，省去音频解码、编码与 ASR 识别，耗时显著下降。

### 6.3 音色管理

```
GET    /api/v1/voices               查看全部
PATCH  /api/v1/voices/{id}          改名 / 修正参考文本
POST   /api/v1/voices/{id}/extract  重新提取特征（force=true）
DELETE /api/v1/voices/{id}          删除
```

---

## 7. 附录

### 7.1 特征复用原理

源音频上传后会被统一转码为单声道 24 kHz wav，并提取出 `VoiceClonePrompt`（声纹特征）保存为 `prompt.pt`。再次使用同一音色时，直接加载该特征参与合成，**不会重复执行**静音裁剪、音频编码与 ASR 识别。因此日志中复用时表现为 `加载已保存特征` 阶段，而不再出现 `声纹特征提取`。

### 7.2 参数调优建议

| 目标 | 建议 |
| --- | --- |
| 追求速度 | `num_step=16`；参考音频控制在 3~10 秒 |
| 追求质量 | `num_step=32`（默认）；参考音频用与目标文本**同语言**的清晰录音 |
| 固定时长输出 | 设置 `duration`（秒），此时忽略 `speed` |
| 调整语速 | `speed`（>1 更快，<1 更慢） |

### 7.3 常见错误排查

| 错误提示 | 原因 | 处理 |
| --- | --- | --- |
| `请求参数错误：待合成的文本不能为空。` | `text` 为空 | 传入非空文本 |
| `请求参数错误：请指定已保存的音色 ID，或临时上传一段参考音频。` | 两者都没传 | 传 `voice_id` 或 `file` |
| `请求参数错误：音色 ID 与临时上传音频只能二选一` | 同时传了两者 | 只保留其一 |
| `请求参数错误：不支持的音频格式：xxx` | 扩展名不在白名单 | 改用受支持的格式 |
| `资源不存在：音色不存在：xxx` | ID 错误或已删除 | 用 `/voices` 重新获取 |
| 返回 422 | 可选数值参数传了空字符串 | 不传该参数即可，不要传空值 |
| 返回 500 | 服务端异常 | 查看容器日志 `docker logs -f omnivoice`，按请求编号检索 |
