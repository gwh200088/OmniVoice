#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OmniVoice 语音克隆服务压测工具。

支持多轮不同并发数对比，输出 QPS、响应时间分位数、排队耗时等指标。

前置依赖：
    pip install httpx

用法示例：

    # 复用已保存音色（生产主要路径，推荐）
    python benchmark.py --url http://127.0.0.1:8000 --voice-id voice_xxx \
        --concurrency 1 2 4 8 --requests 20

    # 先上传音频创建音色，再压测
    python benchmark.py --url http://127.0.0.1:8000 --audio ./ref.wav \
        --auto-create --concurrency 4 --requests 30

    # 临时上传音频（每次重新提取特征，测最坏情况）
    python benchmark.py --url http://127.0.0.1:8000 --audio ./ref.wav \
        --mode upload --concurrency 1 2 --requests 10

    # 结果保存为 JSON
    python benchmark.py --url http://127.0.0.1:8000 --voice-id voice_xxx \
        --concurrency 2 4 --requests 20 --output result.json

建议在服务端同机或同局域网运行，避免网络延迟干扰测试结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:
    import httpx
except ImportError:
    print("缺少依赖 httpx，请先执行：pip install httpx")
    sys.exit(1)


DEFAULT_TEXT = "您好，这里是智能客服，请问有什么可以帮您？祝您生活愉快，再见。"

# 响应头中的服务端耗时字段
HEADER_SERVER_MS = "X-Total-Time-Ms"


@dataclass
class RequestResult:
    """单次请求结果。"""

    success: bool
    client_ms: float           # 客户端观测到的总耗时
    server_ms: float = 0.0     # 服务端报告的耗时（不含排队）
    status_code: int = 0
    error: str = ""


@dataclass
class RoundResult:
    """单轮（某个并发数）压测结果。"""

    concurrency: int
    requests: int
    wall_time_ms: float = 0.0  # 本轮墙钟时间，用于计算真实吞吐
    results: List[RequestResult] = field(default_factory=list)

    @property
    def successes(self) -> List[RequestResult]:
        return [r for r in self.results if r.success]

    @property
    def failures(self) -> List[RequestResult]:
        return [r for r in self.results if not r.success]

    def summary(self) -> dict:
        ok = self.successes
        client_times = [r.client_ms for r in ok]
        server_times = [r.server_ms for r in ok if r.server_ms > 0]
        queue_times = [
            max(0.0, r.client_ms - r.server_ms) for r in ok if r.server_ms > 0
        ]

        # QPS 必须用墙钟时间计算：并发时多个请求同时处理，
        # 用"各请求耗时之和"会严重低估吞吐
        wall_seconds = self.wall_time_ms / 1000.0
        qps = (len(ok) / wall_seconds) if ok and wall_seconds > 0 else 0.0

        return {
            "并发数": self.concurrency,
            "请求总数": self.requests,
            "成功数": len(ok),
            "失败数": len(self.failures),
            "成功率(%)": round(len(ok) / self.requests * 100, 1) if self.requests else 0.0,
            "QPS": round(qps, 3),
            "客户端平均耗时(ms)": _mean(client_times),
            "客户端P50(ms)": _percentile(client_times, 50),
            "客户端P95(ms)": _percentile(client_times, 95),
            "客户端P99(ms)": _percentile(client_times, 99),
            "客户端最大(ms)": round(max(client_times), 1) if client_times else 0.0,
            "服务端平均耗时(ms)": _mean(server_times),
            "服务端P95(ms)": _percentile(server_times, 95),
            "排队平均耗时(ms)": _mean(queue_times),
            "排队最大耗时(ms)": round(max(queue_times), 1) if queue_times else 0.0,
        }


def _mean(data: List[float]) -> float:
    return round(statistics.mean(data), 1) if data else 0.0


def _percentile(data: List[float], p: float) -> float:
    """计算分位数（线性插值）。"""
    if not data:
        return 0.0
    ordered = sorted(data)
    k = (len(ordered) - 1) * p / 100.0
    floor = math.floor(k)
    ceil = math.ceil(k)
    if floor == ceil:
        return round(ordered[int(k)], 1)
    value = ordered[floor] * (ceil - k) + ordered[ceil] * (k - floor)
    return round(value, 1)


def resolve_output_path(raw: str) -> Path:
    """确定结果文件保存路径。

    - 传入已存在的目录：在该目录下生成带时间戳的文件名，便于多次压测对比
    - 传入文件路径：直接使用（父目录不存在时自动创建）
    """
    path = Path(raw)
    if path.is_dir():
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return path / f"benchmark_{stamp}.json"
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build_payload(
    rounds: List[RoundResult],
    bench: "Benchmark",
    args: argparse.Namespace,
    elapsed_total: float,
) -> dict:
    """组装 JSON 结果：既含汇总，也含每次请求的原始数据，便于生成统计图表。"""
    return {
        "压测配置": {
            "服务地址": bench.base_url,
            "压测模式": "复用已保存音色" if args.mode == "reuse" else "临时上传音频",
            "文本长度": len(args.text),
            "每轮请求数": args.requests,
            "并发梯度": list(args.concurrency),
            "单请求超时(秒)": args.timeout,
            "音色ID": bench.voice_id if args.mode == "reuse" else "-",
            "开始时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "汇总": [item.summary() for item in rounds],
        "原始数据": [
            {
                "并发数": item.concurrency,
                "请求明细": [
                    {
                        "序号": index + 1,
                        "成功": one.success,
                        "客户端耗时(ms)": round(one.client_ms, 1),
                        "服务端耗时(ms)": round(one.server_ms, 1),
                        "排队耗时(ms)": round(
                            max(0.0, one.client_ms - one.server_ms), 1
                        ),
                        "状态码": one.status_code,
                        "错误": one.error,
                    }
                    for index, one in enumerate(item.results)
                ],
            }
            for item in rounds
        ],
        "压测总耗时(秒)": round(elapsed_total, 1),
    }


def _fmt_errors(results: List[RequestResult], limit: int = 3) -> str:
    """汇总失败原因。"""
    errors: dict = {}
    for item in results:
        key = item.error or f"HTTP {item.status_code}"
        errors[key] = errors.get(key, 0) + 1
    items = sorted(errors.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return "；".join(f"{msg} × {count}" for msg, count in items)


class Benchmark:
    """压测执行器。"""

    def __init__(
        self,
        base_url: str,
        api_prefix: str,
        voice_id: str,
        audio_path: Optional[str],
        mode: str,
        text: str,
        timeout: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = "/" + api_prefix.strip("/")
        self.voice_id = voice_id
        self.audio_path = audio_path
        self.mode = mode
        self.text = text
        self.timeout = timeout
        self._audio_bytes: Optional[bytes] = None
        self._audio_name: str = "ref.wav"

    @property
    def tts_url(self) -> str:
        return f"{self.base_url}{self.api_prefix}/tts"

    @property
    def voices_url(self) -> str:
        return f"{self.base_url}{self.api_prefix}/voices"

    def _load_audio(self) -> None:
        if self.audio_path and self._audio_bytes is None:
            path = Path(self.audio_path)
            self._audio_bytes = path.read_bytes()
            self._audio_name = path.name

    def create_voice(self, name: str = "压测音色") -> str:
        """上传音频创建音色，返回音色 ID。"""
        self._load_audio()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self.voices_url,
                data={"name": name, "extract": "true"},
                files={"file": (self._audio_name, self._audio_bytes)},
            )
            resp.raise_for_status()
            voice_id = resp.json()["音色信息"]["音色ID"]
        print(f"已创建音色：{voice_id}")
        return voice_id

    async def _one_request(self, client: httpx.AsyncClient, sem: asyncio.Semaphore) -> RequestResult:
        """执行一次合成请求。"""
        async with sem:
            data = {"text": self.text}
            files = None

            if self.mode == "upload":
                self._load_audio()
                files = {"file": (self._audio_name, self._audio_bytes)}
            else:
                data["voice_id"] = self.voice_id

            started = time.perf_counter()
            try:
                resp = await client.post(self.tts_url, data=data, files=files)
                client_ms = (time.perf_counter() - started) * 1000.0
                if resp.status_code == 200:
                    try:
                        server_ms = float(resp.headers.get(HEADER_SERVER_MS, 0) or 0)
                    except ValueError:
                        server_ms = 0.0
                    return RequestResult(
                        success=True,
                        client_ms=client_ms,
                        server_ms=server_ms,
                        status_code=resp.status_code,
                    )
                return RequestResult(
                    success=False,
                    client_ms=client_ms,
                    status_code=resp.status_code,
                    error=f"HTTP {resp.status_code}: {resp.text[:120]}",
                )
            except Exception as exc:
                client_ms = (time.perf_counter() - started) * 1000.0
                return RequestResult(
                    success=False,
                    client_ms=client_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def run_round(self, concurrency: int, requests: int) -> RoundResult:
        """执行一轮压测。"""
        sem = asyncio.Semaphore(concurrency)
        round_result = RoundResult(concurrency=concurrency, requests=requests)

        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            tasks = [
                self._one_request(client, sem) for _ in range(requests)
            ]
            round_result.results = await asyncio.gather(*tasks)
        round_result.wall_time_ms = (time.perf_counter() - started) * 1000.0

        return round_result


def print_round(result: RoundResult) -> None:
    """打印单轮结果。"""
    data = result.summary()
    print("-" * 78)
    print(
        f"并发 {data['并发数']:>3} | 请求 {data['请求总数']:>3} | "
        f"成功 {data['成功数']:>3} | 失败 {data['失败数']:>3} | "
        f"成功率 {data['成功率(%)']:>5}% | QPS {data['QPS']:>6}"
    )
    print(
        f"        客户端耗时：平均 {data['客户端平均耗时(ms)']:>8.1f} ms | "
        f"P50 {data['客户端P50(ms)']:>8.1f} | P95 {data['客户端P95(ms)']:>8.1f} | "
        f"P99 {data['客户端P99(ms)']:>8.1f} | 最大 {data['客户端最大(ms)']:>8.1f}"
    )
    if data["服务端平均耗时(ms)"] > 0:
        print(
            f"        服务端耗时：平均 {data['服务端平均耗时(ms)']:>8.1f} ms | "
            f"P95 {data['服务端P95(ms)']:>8.1f}    "
            f"排队耗时：平均 {data['排队平均耗时(ms)']:>8.1f} ms | "
            f"最大 {data['排队最大耗时(ms)']:>8.1f}"
        )
    if result.failures:
        print(f"        失败原因：{_fmt_errors(result.failures)}")


def print_report(rounds: List[RoundResult], elapsed_total: float) -> None:
    """打印汇总报告。"""
    print("\n" + "=" * 78)
    print("压测汇总（并发能力对比）")
    print("=" * 78)
    header = (
        f"{'并发':>4} | {'成功/总数':>9} | {'QPS':>7} | "
        f"{'平均(ms)':>9} | {'P95(ms)':>9} | {'最大(ms)':>9} | {'排队(ms)':>9}"
    )
    print(header)
    print("-" * 78)
    for item in rounds:
        data = item.summary()
        print(
            f"{data['并发数']:>4} | "
            f"{str(data['成功数']) + '/' + str(data['请求总数']):>9} | "
            f"{data['QPS']:>7} | "
            f"{data['客户端平均耗时(ms)']:>9.1f} | "
            f"{data['客户端P95(ms)']:>9.1f} | "
            f"{data['客户端最大(ms)']:>9.1f} | "
            f"{data['排队平均耗时(ms)']:>9.1f}"
        )
    print("=" * 78)
    print(f"压测总耗时：{elapsed_total:.1f} 秒")
    print()
    print("指标说明：")
    print("  - QPS        ：每秒完成的请求数，受并发上限限制（≈ 并发上限 / 单次合成耗时）")
    print("  - 平均/P95   ：客户端观测到的响应耗时分布")
    print("  - 排队耗时   ：客户端耗时 - 服务端耗时，反映请求等待推理锁的时间")
    print("  - 超过承载能力后继续加压不会提升 QPS，只会增加排队时间（见文档 9.1 并发模型）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OmniVoice 语音克隆服务压测工具",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址")
    parser.add_argument("--api-prefix", default="/api/v1", help="接口前缀")
    parser.add_argument("--voice-id", default="", help="已保存的音色 ID")
    parser.add_argument("--audio", default="", help="参考音频路径")
    parser.add_argument(
        "--auto-create",
        action="store_true",
        help="用 --audio 创建音色后再压测（用于没有现成音色时）",
    )
    parser.add_argument("--mode", choices=["reuse", "upload"], default="reuse",
                        help="reuse=复用已保存音色；upload=每次临时上传音频")
    parser.add_argument("--text", default=DEFAULT_TEXT, help="待合成的文本")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4],
                        help="并发数，可指定多轮，如 1 2 4 8")
    parser.add_argument("--requests", type=int, default=20, help="每轮请求总数")
    parser.add_argument("--timeout", type=float, default=600.0, help="单请求超时（秒）")
    parser.add_argument("--warmup", type=int, default=1, help="预热请求数（不计入统计）")
    parser.add_argument(
        "--output",
        default="",
        help="结果保存路径（JSON）。传目录则在目录下生成带时间戳的文件名，"
             "便于多次压测结果对比；建议指向挂载到宿主机的目录",
    )
    return parser


async def async_main(args: argparse.Namespace) -> int:
    bench = Benchmark(
        base_url=args.url,
        api_prefix=args.api_prefix,
        voice_id=args.voice_id,
        audio_path=args.audio or None,
        mode=args.mode,
        text=args.text,
        timeout=args.timeout,
    )

    # 准备音色
    if args.mode == "reuse":
        if args.auto_create and args.audio:
            bench.voice_id = bench.create_voice()
        elif not bench.voice_id:
            # 未指定时取列表中的第一个音色
            with httpx.Client(timeout=args.timeout) as client:
                resp = client.get(bench.voices_url)
                resp.raise_for_status()
                voices = resp.json().get("音色列表", [])
            if not voices:
                print("未找到任何音色，请先上传音频或用 --audio --auto-create 创建。")
                return 1
            bench.voice_id = voices[0]["音色ID"]
            print(f"使用现有音色：{bench.voice_id}（{voices[0].get('音色名称', '')}）")
    elif not args.audio:
        print("upload 模式必须通过 --audio 指定参考音频。")
        return 1

    print()
    print("=" * 78)
    print("OmniVoice 语音克隆服务压测")
    print("=" * 78)
    print(f"服务地址 ：{bench.base_url}")
    print(f"压测模式 ：{'复用已保存音色' if args.mode == 'reuse' else '临时上传音频'}")
    print(f"文本长度 ：{len(args.text)} 字")
    print(f"并发梯度 ：{args.concurrency}")
    print(f"每轮请求 ：{args.requests}")
    if args.mode == "reuse":
        print(f"音色 ID  ：{bench.voice_id}")

    # 预热（触发模型加载等首次开销）
    if args.warmup > 0:
        print(f"\n预热中（{args.warmup} 次，不计入统计）...")
        warmup = await bench.run_round(concurrency=1, requests=args.warmup)
        failed = len(warmup.failures)
        if failed:
            print(f"预热失败 {failed} 次：{_fmt_errors(warmup.failures)}")
        else:
            print("预热完成。")

    rounds: List[RoundResult] = []
    started = time.perf_counter()
    for concurrency in args.concurrency:
        print(f"\n>>> 开始压测：并发 {concurrency}，请求 {args.requests}")
        result = await bench.run_round(concurrency, args.requests)
        rounds.append(result)
        print_round(result)

    elapsed_total = time.perf_counter() - started
    print_report(rounds, elapsed_total)

    if args.output:
        path = resolve_output_path(args.output)
        payload = build_payload(rounds, bench, args, elapsed_total)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已保存到：{path}")

    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except httpx.HTTPError as exc:
        print(f"连接服务失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
