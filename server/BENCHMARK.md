# OmniVoice 语音克隆服务 · 压测操作指南

本文档说明服务启动后如何做性能压测：从启动容器、确认就绪，到执行压测、观察资源、解读结果、按结果调优。

> 相关文档：
> - 部署、配置与并发模型说明：[`README.md`](README.md)
> - 接口说明：[`API.md`](API.md)
> - 并发模型原理：[`README.md` 9.1 节](README.md)

---

## 1. 压测要回答的问题

压测主要回答三件事：

1. **单条请求有多快**：并发 1 时的响应时间，作为基线
2. **能扛多少并发**：QPS 随并发变化的曲线，以及排队耗时开始显著增长的拐点
3. **瓶颈在哪**：是算力饱和（GPU/CPU 打满），还是并发受限（排队）

---

## 2. 前置准备

### 2.1 确认镜像包含最新代码

若镜像是在并发改造（推理槽位）之前构建的，压测将只能看到串行行为。建议用增量方式更新一次（约 3 秒，不重装依赖）：

```bash
cd <仓库根目录>
docker build -f server/Dockerfile.quick -t omnivoice-service:quick .
docker tag omnivoice-service:quick omnivoice-service:latest
```

### 2.2 压测脚本的运行位置

压测脚本是**纯 HTTP 客户端**，在任何能访问服务的机器上都能运行，**但强烈推荐在部署服务的服务器本机运行**。

| 运行位置 | 网络延迟 | 适用情况 |
| --- | --- | --- |
| **服务端本机**（推荐） | ~0.1 ms，可忽略 | 测响应时间、QPS、并发拐点，数据最准 |
| 同局域网其他机器 | 0.5~2 ms | 可用，数据仍较准 |
| 跨机房 / 远程办公网 | 10 ms 以上，甚至上百 ms | 只能看连通性与大致量级，**延迟数据不可用** |

原因：响应时间 = 网络往返 + 服务端处理 + 排队。远程压测时网络延迟会混入，
导致测出的延迟偏大，尤其 GPU 场景（单条仅 1~2 秒）时偏差更明显。

> **注意**：不要在服务容器内部跑压测——会与被测服务争抢 CPU，反而使结果失真。
> 应在服务器**宿主机**上运行脚本。

### 2.3 依赖要求

压测脚本**只需要 Python 与 httpx**，不需要 torch、不需要 omnivoice，
也不依赖本项目的其他组件。

| 项目 | 要求 |
| --- | --- |
| Python | 3.8 及以上（服务器通常自带 3.6+，请先确认） |
| 第三方包 | 仅 `httpx`（及其自动依赖） |

联网环境安装：

```bash
pip install httpx
```

**内网环境（无外网）离线安装**：在能联网的机器上下载后拷贝到服务器：

```bash
# 有网的机器：下载 httpx 及其全部依赖到目录
pip download httpx -d ./httpx_pkg

# 拷贝到服务器后离线安装
pip install --no-index --find-links=./httpx_pkg httpx
```

### 2.4 放置脚本与测试音频

```bash
# 把脚本与参考音频放到服务器
mkdir -p /opt/bench
cp server/tools/benchmark.py /opt/bench/
cp ref.wav /opt/bench/
```

参考音频建议 3~10 秒、清晰人声，用于创建测试音色。

### 2.5 容器内压测详细步骤（服务器无 Python 环境）

**镜像内已自带 Python 3.10 与 httpx**（随服务依赖安装），
且**压测脚本已包含在镜像中**（`/opt/omnivoice-service/tools/benchmark.py`），
因此即使服务器宿主机没有 Python，也可以直接进入容器压测，无需拷贝脚本、无需安装任何东西。

#### 2.5.1 先看清容器内的目录结构

```
/opt/omnivoice-service/          ← 服务主目录（容器默认工作目录，登录后就在这里）
├── app/                         ← 服务代码（main.py / engine.py / slot.py 等）
├── tools/
│   └── benchmark.py             ← 压测脚本（镜像自带，无需拷贝）
├── data/                        ← 数据目录（通常已挂载到宿主机）
│   ├── voices/                  ← 音色与特征文件
│   ├── outputs/                 ← 合成音频结果
│   ├── logs/                    ← 服务日志
│   └── tmp/                     ← 临时文件
├── requirements.txt             ← 依赖清单
├── README.md / API.md / BENCHMARK.md   ← 文档
└── docker-entrypoint.sh         ← 启动脚本
```

#### 2.5.2 第 1 步：进入容器

```bash
docker exec -it omnivoice /bin/bash
```

进入后默认就在 `/opt/omnivoice-service`（可用 `pwd` 确认）。

#### 2.5.3 第 2 步（可选）：只在还没有音色时才需要

> **如果已经有音色 ID，直接跳过本步和第 ① 步，从 ② 开始即可**——压测复用已保存的特征，不需要再上传音频。

没有音色时，才需要拷一份参考音频进容器（3~10 秒清晰人声）：

```bash
docker cp ./ref.wav omnivoice:/opt/omnivoice-service/data/ref.wav
```

#### 2.5.4 第 3 步：进入脚本目录并压测（在容器内执行）

```bash
cd /opt/omnivoice-service/tools
```

**① 创建测试音色**（**已有音色 ID 时跳过**，只需做一次）：

```bash
python3 benchmark.py \
  --url http://127.0.0.1:8000 \
  --audio /opt/omnivoice-service/data/ref.wav \
  --auto-create \
  --concurrency 1 --requests 3
```

输出中的 `音色 ID` 记下来，下一步要用。
（已有音色也可用 `curl http://127.0.0.1:8000/api/v1/voices` 查询。）

**② 单并发测基线**：

```bash
python3 benchmark.py \
  --url http://127.0.0.1:8000 \
  --voice-id voice_20260929_xxxxxx \
  --concurrency 1 --requests 10
```

**③ 多档并发对比（核心）**，结果存到挂载目录：

```bash
python3 benchmark.py \
  --url http://127.0.0.1:8000 \
  --voice-id voice_20260929_xxxxxx \
  --concurrency 1 4 8 16 \
  --requests 20 \
  --output /opt/bench/
```

CPU 模式请加 `--timeout 1800`（单次耗时可能达分钟级）。

##### 这条命令每个参数分别是什么意思

| 参数 | 本例取值 | 含义 | 说明 |
| --- | --- | --- | --- |
| `--url` | `http://127.0.0.1:8000` | **服务在哪** | 告诉脚本去哪里发请求。在容器内压测时服务就在同一个容器里，所以用 `127.0.0.1`（本机回环）；`8000` 是服务端口，与启动时的 `-p 8000:8000` 一致。脚本会自动拼接成 `http://127.0.0.1:8000/api/v1/tts` 等接口地址 |
| `--voice-id` | `voice_20260929_xxxxxx` | **用哪个音色合成** | 指定已保存的音色。压测会直接复用它已提取好的特征，**不再上传音频、不再重新提取**，因此测的就是生产主要路径。可换成你自己的 ID（用 `curl http://127.0.0.1:8000/api/v1/voices` 查询）；不传则自动取列表第一个 |
| `--concurrency` | `1 4 8 16` | **并发梯度，跑 4 轮** | 空格分隔的多个值，脚本会**依次**跑多轮：同时发 1 → 4 → 8 → 16 个请求，结果在汇总表里并排对比。启用批量推理后，并发的作用是**给服务端攒批"喂料"**（不再决定 GPU 并行度），所以档位要覆盖 `BATCH_MAX_SIZE` 的上下：并发 1 是无批量基线，4 刚好填满一批，8/16 用于确认批量上限是否成为新瓶颈 |
| `--requests` | `20` | **每轮发多少个请求** | 每一轮总共发 20 次合成请求（不是总数）。本例 4 轮 × 20 = **共 80 次请求**。建议先设 10 试跑，确认单轮耗时可接受再加大 |
| `--output` | `/opt/bench/` | **结果存到哪（容器内路径）** | 传的是**目录**而非文件，脚本会自动在其中生成 `benchmark_<日期>_<时间>.json`。它对应宿主机的哪个目录，取决于启动容器时的 `-v` 挂载，见 2.5.6 |

##### 没写出来、但实际生效的默认参数

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--mode` | `reuse` | 复用已保存音色（本例行为）。若改成 `upload` 则每次临时上传音频、重新提取特征，用于测最坏情况 |
| `--text` | 内置 30 字文本 | 每次请求合成的文本内容。**建议换成你的真实业务文本**，否则测的是内置文本的长度 |
| `--api-prefix` | `/api/v1` | 接口前缀，与服务的 `API_PREFIX` 一致 |
| `--timeout` | `600` | 单个请求超时 600 秒；CPU 模式需调大 |
| `--warmup` | `1` | 压测前先发 1 次预热请求（不计入统计），用于触发模型加载 |

##### 这条命令会跑多久

每轮耗时 ≈（每轮请求数 ÷ 该轮实际并发）× 单次合成耗时，其中实际并发受服务 `MAX_CONCURRENCY` 限制。

以单次合成 2 秒、服务并发上限 2 为例：

| 并发档位 | 实际并发 | 该轮耗时 |
| --- | --- | --- |
| 1 | 1 | 20 × 2 = 40 秒 |
| 2 | 2 | 10 × 2 = 20 秒 |
| 4 | 2（被上限限制） | 20 秒 |
| 8 | 2（被上限限制） | 20 秒 |
| **合计** | | **约 100 秒** + 模型加载预热时间 |

> 若并发 4、8 与并发 2 耗时相同，说明服务并发已达上限，继续加压没有意义——这正是压测要找的结论。

> `--output /opt/bench/` 是**容器内路径**，对应的宿主机目录取决于启动容器时的挂载参数，见 2.5.6。

#### 2.5.5 结果文件在哪、叫什么

- **不加 `--output`**：只在屏幕打印，不生成文件
- **加 `--output` 传目录**：在目录下自动生成文件，命名格式为

  ```
  benchmark_<日期>_<时间>.json
  例：benchmark_20260929_143052.json
  ```

  时间戳保证多次压测结果不会互相覆盖，便于对比
- **加 `--output` 传文件路径**：直接写入该路径

#### 2.5.6 结果目录：容器内路径 ↔ 宿主机路径（重要）

压测结果写在**容器内**，要能在宿主机看到，必须靠启动容器时的 `-v` 挂载。
路径对应关系完全取决于你的挂载参数：

| 启动时的挂载参数 | 容器内写这里 | 宿主机实际位置 |
| --- | --- | --- |
| `-v /data/omnivoice/data:/opt/omnivoice-service/data` | `/opt/omnivoice-service/data/bench/` | `/data/omnivoice/data/bench/` |
| `-v /data/bench:/opt/bench` | `/opt/bench/` | `/data/bench/` |
| 未挂载任何结果目录 | 任意容器路径 | 需用 `docker cp` 拷出 |

**推荐做法**：启动容器时单独挂一个结果目录，路径清晰不与其他数据混在一起：

```bash
docker run -d --name omnivoice ... \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/models:/opt/models \
  -v /data/bench:/opt/bench \          # ← 压测结果目录
  ...
```

压测时用：

```bash
python3 benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> --concurrency 1 4 8 16 --requests 20 \
  --output /opt/bench/
```

结果就在宿主机的 `/data/bench/benchmark_<日期>_<时间>.json`，直接可取用。

**若容器已经启动、没挂载结果目录**，两种补救办法：

```bash
# 办法 A：不挂载，压完再拷出来
python3 benchmark.py ... --output /tmp/bench/          # 容器内执行
docker cp omnivoice:/tmp/bench/benchmark_20260929_143052.json /data/bench/   # 宿主机执行
```

```bash
# 办法 B：重启容器并补上挂载（数据目录已持久化，重启不影响音色与特征）
docker stop omnivoice && docker rm omnivoice
docker run -d --name omnivoice ... -v /data/bench:/opt/bench ...
```

> 注意：压测脚本会自动创建不存在的目录，无需提前 `mkdir`。

#### 2.5.7 不进入容器的等价写法

不想 `docker exec -it` 交互式进入，可直接一条命令跑完：

```bash
docker exec omnivoice python3 /opt/omnivoice-service/tools/benchmark.py \
  --url http://127.0.0.1:8000 \
  --voice-id voice_20260929_xxxxxx \
  --concurrency 1 4 8 16 --requests 20 \
  --output /opt/omnivoice-service/data/bench/
```

#### 2.5.8 方式二：独立压测容器（数据更严谨）

用一个临时容器专门跑压测，避免与被测服务争抢 CPU：

```bash
docker run --rm --network host \
  -v /data/bench:/opt/bench \
  --entrypoint python3 \
  omnivoice-service:latest \
  /opt/omnivoice-service/tools/benchmark.py \
  --url http://127.0.0.1:8000 \
  --voice-id <音色ID> \
  --concurrency 1 4 8 16 --requests 20 \
  --output /opt/bench/
```

- `--entrypoint python3`：覆盖启动脚本，不启动服务，只运行压测
- `--network host`（Linux）：容器内 `127.0.0.1` 即宿主机
- `--rm`：压测结束自动清理容器

> Windows / macOS 的 Docker Desktop 不支持 `--network host`，
> 请改用 `--add-host=host.docker.internal:host-gateway`，
> 并把 `--url` 换成 `http://host.docker.internal:8000`。

**方式二：启动独立压测容器（数据更严谨）**

用一个临时容器专门跑压测，避免与被测服务争抢 CPU：

```bash
docker run --rm --network host \
  -v /opt/bench:/bench \
  --entrypoint python3 \
  omnivoice-service:latest \
  /bench/benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> --concurrency 1 4 8 16 --requests 20
```

- `--entrypoint python3`：覆盖启动脚本，不启动服务，只运行压测
- `--network host`（Linux）：容器内 `127.0.0.1` 即宿主机
- `--rm`：压测结束后自动清理容器

> 压测脚本本身的 CPU 开销很小（主要是等待 HTTP 响应），方式一对结果的影响通常可忽略；
> 若需要最严谨的数据，用方式二。

> Windows / macOS 的 Docker Desktop 不支持 `--network host`，
> 请改用 `--add-host=host.docker.internal:host-gateway`，
> 并把 `--url` 换成 `http://host.docker.internal:8000`。

---

## 3. 启动服务

### 3.1 GPU 环境（T4 / A10 / A100）

```bash
docker run -d --name omnivoice --gpus '"device=0"' -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/models:/opt/models \
  -e MODEL_ID=/opt/models/OmniVoice \
  -e DEVICE=cuda:0 \
  -e DTYPE=float16 \
  -e HF_HUB_OFFLINE=1 \
  -e MAX_CONCURRENCY=2 \
  omnivoice-service:latest
```

### 3.2 CPU 环境（无显卡）

```bash
docker run -d --name omnivoice -p 8000:8000 \
  -v /data/omnivoice/data:/opt/omnivoice-service/data \
  -v /data/models:/opt/models \
  -e MODEL_ID=/opt/models/OmniVoice \
  -e DEVICE=cpu \
  -e DTYPE=float32 \
  -e DEFAULT_NUM_STEP=16 \
  -e LOAD_ASR=false \
  -e HF_HUB_OFFLINE=1 \
  -e MAX_CONCURRENCY=1 \
  omnivoice-service:latest
```

> CPU 模式说明与提速方法见 [`README.md` 5.1](README.md#51-cpu-模式部署无显卡环境)。

### 3.3 确认服务就绪

```bash
# 查看启动日志，等待模型加载完成
docker logs -f omnivoice

# 健康检查（不加载模型，返回极快）
curl http://127.0.0.1:8000/api/v1/health

# 查看服务信息：引擎、并发槽位、显存
curl http://127.0.0.1:8000/api/v1/info
```

`info` 返回的 `引擎信息.并发槽位` 可确认并发相关参数是否生效：

| 字段 | 说明 |
| --- | --- |
| `最大并发数` | `MAX_CONCURRENCY` 是否生效（0 为自动：GPU 2 / CPU 1） |
| `批量推理` | 批量调度状态：单批上限、已处理批次数、历史最大批量、平均批量 |
| `单次推理显存估算(MB)` | 自适应学习到的值，或手动指定的值 |
| `可分配显存容量(MB)` | 缓存池空闲 + 设备级剩余 |
| `GPU利用率(%)` | 有数值说明 `pynvml` 可用；显示"不可用"表示未安装 |

---

## 4. 执行压测

### 4.1 准备测试音色

```bash
python benchmark.py --url http://127.0.0.1:8000 \
  --audio ./ref.wav --auto-create --concurrency 1 --requests 3
```

输出中的 `音色 ID` 用于后续压测。若已有音色，可用 `GET /api/v1/voices` 查询。

### 4.2 单并发基线（必做）

```bash
python benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> --concurrency 1 --requests 10
```

得到单次合成的耗时基线，用于推算 QPS 上限。注意启用批量推理后，决定吞吐的
是**批量上限**而不是并发数：`QPS上限 ≈ BATCH_MAX_SIZE ÷ 单批耗时`
（关闭批量时才是 `并发数 ÷ 单次耗时`）。

### 4.3 多档并发对比（核心）

```bash
python benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> \
  --concurrency 1 4 8 16 \
  --requests 20 \
  --output result.json
```

**档位为什么取 1 / 4 / 8 / 16**：启用批量推理后，并发的作用变了——它不再决定
GPU 的并行度（kernel 在同一 stream 内串行，加并发不会加速），而是**给服务端的
攒批机制"喂料"**。

| 档位 | 含义 | 预期结果 |
| --- | --- | --- |
| 1 | 无批量基线，走单条推理 | QPS 最低，作为对照组 |
| 4 | 刚好填满一批（`BATCH_MAX_SIZE` 默认 4） | QPS 应明显提升 |
| 8 / 16 | 超过批量上限，多余请求排队 | QPS 基本不再上涨 |

若 4 → 8 时 QPS 仍在涨，说明批量上限还有空间，可把 `BATCH_MAX_SIZE`
提到 6 或 8 再测（留意显存占用）。

> CPU 模式单次耗时可能达分钟级，请调大超时：`--timeout 1800`。

### 4.4 其他压测场景

| 场景 | 命令要点 |
| --- | --- |
| 临时上传音频（每次重新提取特征，最坏情况） | `--mode upload --audio ./ref.wav` |
| 长文本（更贴近真实业务） | `--text "<实际业务文本>"` |
| 压测已有的音色而不重新创建 | 只传 `--voice-id`，不加 `--auto-create` |

### 4.5 压测生成的音频文件存在哪（重要）

**压测脚本本身不保存音频**——它只统计耗时，收到的音频数据直接丢弃，唯一产物是 `--output` 指定的 JSON 统计文件。

但**服务端默认会保存每次合成的音频**（`SAVE_OUTPUT` 默认为 `true`）：

| 项目 | 路径 |
| --- | --- |
| 容器内 | `/opt/omnivoice-service/data/outputs/` |
| 文件名 | `<日期>_<请求编号>.wav`，如 `20260929_tts-1a2b3c4d.wav` |
| 宿主机 | 取决于 `data` 目录的挂载，例如 `/data/omnivoice/data/outputs/` |

因此一次 80 条请求的压测会留下 80 个 wav（23 秒音频约 1.1 MB/条，合计约 90 MB），
既占磁盘，写盘耗时也会计入总耗时。

**建议做法**：

```bash
# 压测时关掉落盘，得到更纯粹的推理耗时（推荐）
docker run -d ... -e SAVE_OUTPUT=false ...

# 或压测后清理
docker exec omnivoice sh -c "rm -f /opt/omnivoice-service/data/outputs/*.wav"

# 查看数量与占用
docker exec omnivoice sh -c "ls /opt/omnivoice-service/data/outputs/ | wc -l; \
  du -sh /opt/omnivoice-service/data/outputs/"
```

若需要留几条试听效果，按上表的文件名格式挑选保留即可；
也可用接口 `GET /api/v1/outputs` 列出、`GET /api/v1/outputs/{文件名}` 下载。

---

## 5. 压测时同步观察资源

另开一个终端执行：

```bash
# GPU 利用率与显存（每秒刷新）
nvidia-smi dmon

# 或简洁版
watch -n 1 nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv

# 容器 CPU / 内存占用
docker stats omnivoice
```

**关键判断**：加并发本身对 GPU 推理基本无效（kernel 在同一 CUDA stream 内串行执行），
这里主要用来确认算力是否饱和——利用率长期 95%+ 即已饱和。
真正决定吞吐的是批量大小，请看 `/api/v1/info` 的 `批量推理.平均批量`。

---

## 6. 结果解读

### 6.1 输出示例

```
==============================================================================
压测汇总（并发能力对比）
==============================================================================
并发 |  成功/总数 |     QPS |   平均(ms) |   P95(ms) |   最大(ms) |   排队(ms)
------------------------------------------------------------------------------
   1 |     20/20 |   0.512 |     1953.2 |    2100.5 |     2160.3 |        0.0
   2 |     20/20 |   0.981 |     2035.4 |    2350.1 |     2600.2 |      120.3
   4 |     20/20 |   1.024 |     3900.6 |    4500.8 |     5100.4 |     1850.7
   8 |     18/20 |   1.031 |     7700.2 |    8900.3 |    15200.1 |     5800.9
==============================================================================
```

### 6.2 指标含义

| 指标 | 含义 | 用途 |
| --- | --- | --- |
| `QPS` | 每秒完成请求数（按墙钟时间计算） | 吞吐能力 |
| `平均 / P95 / 最大` | 客户端观测到的响应耗时分布 | 评估用户实际体验 |
| `排队` | 客户端耗时 − 服务端耗时 | 等待推理槽位的时间，**判断承载上限的关键** |

### 6.3 判断标准

| 现象 | 结论 | 建议 |
| --- | --- | --- |
| 并发 1→2，QPS 明显提升 | 攒批生效 | 继续调大 `BATCH_MAX_SIZE`（留意显存） |
| 并发 2→4，QPS 不再涨、排队耗时陡增 | 已达承载上限 | 保持当前配置，别再加大批量 |
| QPS 不涨，且 GPU 利用率已 95%+ | 算力饱和 | 降 `DEFAULT_NUM_STEP`，或加显卡 |
| QPS 不涨，且 `平均批量` 接近 1 | 没攒起来 | 调大 `BATCH_WAIT_MS`，或提高压测并发 |
| 出现失败（超时） | 超出承载 | 降并发 / 批量，或调大 `SLOT_WAIT_TIMEOUT` |

以上面的示例数据为例：并发 2 时 QPS 接近翻倍（0.512→0.981），说明并发有效；并发 4 时 QPS 几乎不涨而排队涨到 1850 ms，说明**实际承载约为 2~3**，再往上只会让用户等更久；并发 8 已开始出现超时。

---

## 7. 结果存储与图表生成

### 7.1 保存结果文件

`--output` 有两种写法：

```bash
# 指定文件：直接写入
--output /opt/bench/result.json

# 指定目录：自动生成带时间戳的文件名，多次压测结果不会互相覆盖，便于对比
--output /opt/bench/results/
# 实际生成：/opt/bench/results/benchmark_20260928_211612.json
```

### 7.2 在容器内压测时把结果落到宿主机

容器内写的文件默认只在容器里，需通过挂载让结果出现在宿主机：

```bash
# 启动服务时挂载一个结果目录
docker run -d --name omnivoice ... -v /data/bench:/opt/bench ...

# 压测结果写入挂载目录，宿主机 /data/bench 下即可看到
docker exec omnivoice python3 /opt/bench/benchmark.py \
  --url http://127.0.0.1:8000 --voice-id <音色ID> \
  --concurrency 1 4 8 16 --requests 20 \
  --output /opt/bench/results/
```

若服务已启动且未挂载目录，可用 `docker cp` 拷出：

```bash
docker cp omnivoice:/opt/bench/results/benchmark_20260928_211612.json ./result.json
```

### 7.3 JSON 结构

```json
{
  "压测配置": {
    "服务地址": "http://127.0.0.1:8000",
    "压测模式": "复用已保存音色",
    "文本长度": 30,
    "每轮请求数": 20,
    "并发梯度": [1, 2, 4],
    "单请求超时(秒)": 600,
    "音色ID": "voice_xxx",
    "开始时间": "2026-09-28 21:16:12"
  },
  "汇总": [
    {
      "并发数": 2, "请求总数": 20, "成功数": 20, "失败数": 0, "成功率(%)": 100.0,
      "QPS": 0.981, "客户端平均耗时(ms)": 2035.4,
      "客户端P50(ms)": 1980.1, "客户端P95(ms)": 2350.1, "客户端P99(ms)": 2500.0,
      "客户端最大(ms)": 2600.2, "服务端平均耗时(ms)": 1900.0,
      "服务端P95(ms)": 2100.0, "排队平均耗时(ms)": 120.3, "排队最大耗时(ms)": 300.5
    }
  ],
  "原始数据": [
    {
      "并发数": 2,
      "请求明细": [
        {
          "序号": 1, "成功": true,
          "客户端耗时(ms)": 2100.5, "服务端耗时(ms)": 1980.2,
          "排队耗时(ms)": 120.3, "状态码": 200, "错误": ""
        }
      ]
    }
  ],
  "压测总耗时(秒)": 123.4
}
```

- `汇总`：每轮一个条目的统计值，适合画**柱状对比图**
- `原始数据`：每一次请求的耗时明细，适合画**散点图、直方图、时间序列图**

### 7.4 生成图表（Python + matplotlib）

```python
import json
import matplotlib.pyplot as plt

data = json.load(open("benchmark_20260928_211612.json", encoding="utf-8"))
summary = data["汇总"]

# 图 1：各并发下的 QPS 对比
plt.figure(figsize=(8, 4))
plt.bar([r["并发数"] for r in summary], [r["QPS"] for r in summary], color="#4C78A8")
plt.xlabel("并发数"); plt.ylabel("QPS"); plt.title("吞吐随并发变化")
plt.savefig("qps.png", dpi=150, bbox_inches="tight")

# 图 2：响应时间（平均 / P95 / 最大）对比
plt.figure(figsize=(8, 4))
x = [r["并发数"] for r in summary]
plt.plot(x, [r["客户端平均耗时(ms)"] for r in summary], "o-", label="平均")
plt.plot(x, [r["客户端P95(ms)"] for r in summary], "s-", label="P95")
plt.plot(x, [r["客户端最大(ms)"] for r in summary], "^-", label="最大")
plt.xlabel("并发数"); plt.ylabel("耗时 (ms)"); plt.legend(); plt.title("响应时间随并发变化")
plt.savefig("latency.png", dpi=150, bbox_inches="tight")

# 图 3：排队耗时（判断承载拐点）
plt.figure(figsize=(8, 4))
plt.plot(x, [r["排队平均耗时(ms)"] for r in summary], "o-", color="#E45756")
plt.xlabel("并发数"); plt.ylabel("排队耗时 (ms)"); plt.title("排队耗时随并发变化")
plt.savefig("queue.png", dpi=150, bbox_inches="tight")
```

> 需要 `pip install matplotlib`。若不想装，也可用下节的 CSV 导出后交给 Excel。

### 7.5 导出 CSV（Excel 分析）

```python
import csv
import json

data = json.load(open("benchmark_20260928_211612.json", encoding="utf-8"))
with open("result.csv", "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["并发数", "序号", "成功", "客户端耗时(ms)", "服务端耗时(ms)", "排队耗时(ms)"])
    for round_item in data["原始数据"]:
        for item in round_item["请求明细"]:
            writer.writerow([
                round_item["并发数"], item["序号"], item["成功"],
                item["客户端耗时(ms)"], item["服务端耗时(ms)"], item["排队耗时(ms)"],
            ])
```

`utf-8-sig` 编码确保 Excel 打开中文表头不乱码。

---

## 8. 根据结果调优

| 目标 | 调整项 |
| --- | --- |
| **提升吞吐（最有效）** | `BATCH_ENABLED=true`（默认开启）+ 调大 `BATCH_MAX_SIZE`（T4 用 4，A10/A100 试 8） |
| 降低单条耗时 | `DEFAULT_NUM_STEP`（32→16→8），QPS 同比提升 |
| 避免无效并发 | `MAX_CONCURRENCY=1`（多线程不加速，只会和批次抢显卡） |
| 避免无效并发 | 安装 `nvidia-ml-py` 后设 `GPU_UTILIZATION_LIMIT=90` |
| 减少首次特征提取开销 | `MAX_REF_DURATION=8`、上传时 `extract=true` 并复用音色 |
| 提升整体吞吐 | 多容器 + 多显卡（`--gpus '"device=1"'`） |

调整后重新跑一次压测对比，确认效果。

---

## 9. 常见问题

**Q：首次压测时前几秒特别慢？**
A：模型加载（CPU 上可能数分钟）未完成。用 `docker logs -f omnivoice` 确认加载完成后再压；压测脚本默认有 1 次预热，也可加大 `--timeout`。

**Q：从我的电脑远程压服务器，数据准吗？**
A：网络往返会混入响应时间，只能看连通性和大致量级。要测真实性能请在服务端同机运行。

**Q：压测期间其他请求会受影响吗？**
A：会。压测占用推理槽位，正常业务请求会排队。建议在业务低峰期压测，或用独立容器。

**Q：只想快速看单次耗时，不想装 httpx？**
A：用 curl 计时即可：

```bash
time curl -X POST "http://127.0.0.1:8000/api/v1/tts" \
  -F "text=您好，这里是智能客服。" \
  -F "voice_id=<音色ID>" -o /dev/null
```

**Q：并发从 1 提到 5，QPS 几乎没变，为什么？**
A：这是正常现象，不是配置问题。GPU 的 kernel 在同一个 CUDA stream 内**串行执行**，
开多个线程各跑一条并不会并行加速，只是让请求轮流使用显卡（实测 0.10 → 0.11）。

提升吞吐要靠**批量推理**（服务默认已开启）：把多条合成打包进一次前向。
请按下一步确认批量是否真的生效。

**Q：怎么看批量是否生效？**
A：两种方式：

1. 查接口——`curl http://127.0.0.1:8000/api/v1/info` 的 `批量推理` 段，
   关注 `平均批量` 与 `历史最大批量`：
   - 接近 1：没攒起来（并发不够，或 `BATCH_WAIT_MS` 太短）
   - 接近 `BATCH_MAX_SIZE`：攒批正常
2. 看日志——会打印"本条与另外 N 条合并为一批推理"，
   引擎侧同步打印"批量语音合成完成：本批 N 条"。

**Q：并发压测时出现 OOM？**
A：降低 `BATCH_MAX_SIZE`（批量越大显存占用越高），或降低 `MAX_CONCURRENCY`、
调大 `GPU_RESERVE_MB` 留出更多显存余量。

**Q：怎么确认服务各模块之间接口是正常的？**
A：运行契约检查脚本。它会**真实导入全部模块**并调用关键路径（批量推理、
批量调度、业务编排、接口路由等），全部通过才返回退出码 0：

```bash
# 在容器内（依赖齐全）
python3 tools/check_contract.py

# 或用镜像直接跑
docker run --rm --entrypoint python3 omnivoice-service:batch \
  /opt/omnivoice-service/tools/check_contract.py
```

建议在改动代码或调整配置后跑一次。它可以提前发现「某模块引用了另一个模块
并不存在的属性/方法」这类问题——这类问题语法检查查不出来，等请求进来才报错。

---

## 附录：压测脚本常用参数速查

| 参数 | 默认值 | 含义 | 示例 / 注意 |
| --- | --- | --- | --- |
| `--url` | `http://127.0.0.1:8000` | 服务访问地址 | 容器内压自己用 `http://127.0.0.1:8000`；独立容器加 `--network host` 后同样写 `127.0.0.1` |
| `--api-prefix` | `/api/v1` | 接口前缀 | 需与服务的 `API_PREFIX` 一致，一般不用改 |
| `--voice-id` | 空 | 已保存的音色 ID | 为空时自动取音色列表第一个；建议显式指定 |
| `--audio` | 空 | 参考音频路径 | 填**容器内路径**，如 `/opt/omnivoice-service/data/ref.wav` |
| `--auto-create` | 关闭 | 先用 `--audio` 创建音色，再压测 | 首次压测、还没有音色时加这个参数 |
| `--mode` | `reuse` | `reuse`=复用已保存音色；`upload`=每次临时上传音频 | `reuse` 是生产主要路径；`upload` 用于测最坏情况 |
| `--text` | 内置 30 字文本 | 待合成的文本 | 建议换成真实业务文本，如 `--text "您好，这里是智能客服……"` |
| `--concurrency` | `1 4 8 16` | 并发梯度，空格分隔，依次测多轮；档位需覆盖批量上限的上下 | `--concurrency 1 4 8 16` |
| `--requests` | `20` | 每轮的请求总数 | 并发越大总耗时越长，可先设 10 试跑 |
| `--timeout` | `600` | 单个请求的超时时间（秒） | CPU 模式建议 `--timeout 1800` |
| `--warmup` | `1` | 预热次数，不计入统计 | 用于触发模型加载；设 `0` 可跳过 |
| `--output` | 空 | 结果保存路径 | 传**目录**则自动生成 `benchmark_<日期>_<时间>.json`；传文件路径则直接写入。不传则只打印不保存 |
| `-h` / `--help` | - | 查看帮助 | `python3 benchmark.py --help` |

**常用组合示例**：

```bash
# 首次：创建音色（记住输出的音色 ID）
python3 benchmark.py --url http://127.0.0.1:8000 \
  --audio /opt/omnivoice-service/data/ref.wav --auto-create \
  --concurrency 1 --requests 3

# 正式压测：4 档并发，结果存到挂载目录
python3 benchmark.py --url http://127.0.0.1:8000 \
  --voice-id voice_20260929_ab12cd \
  --concurrency 1 4 8 16 --requests 20 \
  --output /opt/omnivoice-service/data/bench/

# 临时上传模式（测最坏情况，每次重新提取特征）
python3 benchmark.py --url http://127.0.0.1:8000 \
  --mode upload --audio /opt/omnivoice-service/data/ref.wav \
  --concurrency 1 2 --requests 10
```
