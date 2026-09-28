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

### 2.5 服务器没有 Python 环境时：在容器内压测

**镜像内已自带 Python 3.10 与 httpx**（随服务依赖安装），
因此即使服务器宿主机没有 Python，也可以直接在容器内运行压测脚本，无需任何安装。

**方式一：在服务容器内执行（最简单）**

```bash
# 把脚本与参考音频拷进运行中的容器
docker exec omnivoice mkdir -p /opt/bench
docker cp benchmark.py omnivoice:/opt/bench/
docker cp ref.wav omnivoice:/opt/bench/

# 创建测试音色
docker exec omnivoice python3 /opt/bench/benchmark.py \
  --url http://127.0.0.1:8000 \
  --audio /opt/bench/ref.wav --auto-create --concurrency 1 --requests 3

# 正式压测
docker exec omnivoice python3 /opt/bench/benchmark.py \
  --url http://127.0.0.1:8000 \
  --voice-id <音色ID> \
  --concurrency 1 2 4 --requests 20
```

容器内访问本机服务直接用 `http://127.0.0.1:8000`。

**方式二：启动独立压测容器（数据更严谨）**

用一个临时容器专门跑压测，避免与被测服务争抢 CPU：

```bash
docker run --rm --network host \
  -v /opt/bench:/bench \
  --entrypoint python3 \
  omnivoice-service:latest \
  /bench/benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> --concurrency 1 2 4 --requests 20
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

得到单次合成的耗时基线，用于推算 QPS 上限：`QPS上限 ≈ 并发数 ÷ 单次耗时`。

### 4.3 多档并发对比（核心）

```bash
python benchmark.py --url http://127.0.0.1:8000 \
  --voice-id <音色ID> \
  --concurrency 1 2 4 8 \
  --requests 20 \
  --output result.json
```

> CPU 模式单次耗时可能达分钟级，请调大超时：`--timeout 1800`。

### 4.4 其他压测场景

| 场景 | 命令要点 |
| --- | --- |
| 临时上传音频（每次重新提取特征，最坏情况） | `--mode upload --audio ./ref.wav` |
| 长文本（更贴近真实业务） | `--text "<实际业务文本>"` |
| 压测已有的音色而不重新创建 | 只传 `--voice-id`，不加 `--auto-create` |

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

**关键判断**：看并发提升时 GPU 利用率的变化——这是决定"要不要继续加并发"的依据。

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
| 并发 1→2，QPS 明显提升 | 并发有效 | 可继续提高 `MAX_CONCURRENCY` |
| 并发 2→4，QPS 不再涨、排队耗时陡增 | 已达承载上限 | 保持当前并发，别再加 |
| QPS 不涨，且 GPU 利用率已 95%+ | 算力饱和 | 降 `DEFAULT_NUM_STEP`，或加显卡 |
| QPS 不涨，但 GPU 利用率只有 50% | 并发受限 | 提高 `MAX_CONCURRENCY`（显存允许时） |
| 出现失败（超时） | 并发超出承载 | 降并发，或调大 `SLOT_WAIT_TIMEOUT` |

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
  --concurrency 1 2 4 --requests 20 \
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
| 降低单条耗时 | `DEFAULT_NUM_STEP`（32→16→8），QPS 同比提升 |
| 提高吞吐上限 | `MAX_CONCURRENCY`（需显存有余量） |
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

**Q：并发压测时出现 OOM？**
A：降低 `MAX_CONCURRENCY`，或调大 `GPU_RESERVE_MB` 留出更多显存余量。

---

## 附录：压测脚本常用参数速查

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--url` | `http://127.0.0.1:8000` | 服务地址 |
| `--voice-id` | 空 | 已保存音色 ID；为空时自动取列表第一个 |
| `--audio` | 空 | 参考音频路径 |
| `--auto-create` | 关闭 | 用 `--audio` 创建音色后再压测 |
| `--mode` | `reuse` | `reuse` 复用音色 / `upload` 临时上传 |
| `--text` | 内置文本 | 待合成文本，建议换成实际业务长度 |
| `--concurrency` | `1 2 4` | 并发梯度，可多轮依次测试 |
| `--requests` | `20` | 每轮请求总数 |
| `--timeout` | `600` | 单请求超时（秒），CPU 模式请调大 |
| `--warmup` | `1` | 预热次数，不计入统计 |
| `--output` | 空 | 结果 JSON 保存路径 |
