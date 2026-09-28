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

### 2.2 准备压测环境

压测脚本依赖 `httpx`，**建议在服务端同机运行**（避免网络延迟混入响应时间）：

```bash
pip install httpx

# 把脚本放到服务器，例如
mkdir -p /opt/bench && cp server/tools/benchmark.py /opt/bench/
```

同时准备一段 3~10 秒的参考音频（如 `ref.wav`），用于创建测试音色。

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

## 7. 根据结果调优

| 目标 | 调整项 |
| --- | --- |
| 降低单条耗时 | `DEFAULT_NUM_STEP`（32→16→8），QPS 同比提升 |
| 提高吞吐上限 | `MAX_CONCURRENCY`（需显存有余量） |
| 避免无效并发 | 安装 `nvidia-ml-py` 后设 `GPU_UTILIZATION_LIMIT=90` |
| 减少首次特征提取开销 | `MAX_REF_DURATION=8`、上传时 `extract=true` 并复用音色 |
| 提升整体吞吐 | 多容器 + 多显卡（`--gpus '"device=1"'`） |

调整后重新跑一次压测对比，确认效果。

---

## 8. 常见问题

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
