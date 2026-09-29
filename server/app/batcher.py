"""批量推理调度器。

为什么必须走批量（batch）
------------------------
GPU 上的 kernel 在同一个 CUDA stream 内是**串行执行**的，因此"开多个
线程、每个线程各跑一条"并不会带来并行加速——实测并发 5 相比并发 1 的
QPS 几乎没有提升（0.10 → 0.11），只是让请求排队轮流使用显卡。

真正的加速来自 batch：把多条合成打包进一次 ``model.generate(text=[...])``
调用，让 GPU 在同一次前向里同时处理多条样本。上游 benchmark 显示
batch=8 的 RTF 约为 batch=1 的 3 倍。

本模块把时间窗内到达的请求攒成一批，交给引擎一次性推理：

- 单条 worker 线程串行执行批次，避免多线程争抢显卡；
- 到达的请求先入队，攒够上限或超过等待窗口即触发推理；
- 结果通过 ``threading.Event``（同步调用）或 ``asyncio.Future``
  （异步调用）回传给各自的请求方，因此接口层两种写法都能用。

分组说明
--------
``generation_config``（扩散步数、引导系数、是否降噪等）是**整批共用**的，
无法逐条指定，因此攒批时会按这些参数分组；音色（prompt）、语速、时长
等支持逐条不同，可以混在同一批里。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple

from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class BatchItem:
    """批量推理中的单条请求。"""

    text: str = ""
    prompt: Any = None
    language: str = ""
    instruct: str = ""
    duration: float = 0.0
    speed: float = 0.0
    # ---- generation_config 相关：整批必须一致，用作分组键 ----
    num_step: Optional[int] = None
    guidance_scale: Optional[float] = None
    denoise: bool = True
    preprocess_prompt: bool = True
    postprocess_output: bool = True

    # ---- 以下由调度器回填，调用方不要设置 ----
    result: Any = None
    error: Optional[BaseException] = None
    wait_ms: float = 0.0
    batch_size: int = 0

    _event: Any = field(default=None, repr=False)
    _future: Any = field(default=None, repr=False)
    _loop: Any = field(default=None, repr=False)

    def group_key(self) -> Tuple:
        """分组键：同一批内 generation 参数必须一致，音色有无也要一致。"""
        return (
            self.num_step,
            self.guidance_scale,
            self.denoise,
            self.preprocess_prompt,
            self.postprocess_output,
            self.prompt is not None,
        )


class BatchScheduler:
    """把并发请求攒成批次，交给引擎批量推理。"""

    def __init__(
        self,
        engine: Any,
        max_size: int = 4,
        wait_ms: float = 50.0,
        enabled: bool = True,
        timeout: float = 300.0,
    ) -> None:
        self.engine = engine
        self.max_size = max(1, int(max_size))
        self.wait_ms = max(0.0, float(wait_ms))
        self.enabled = bool(enabled)
        self.timeout = max(1.0, float(timeout))

        self._queue: "Queue[BatchItem]" = Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._batches = 0
        self._items = 0
        self._peak = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 worker 线程（未启用批量时什么都不做）。"""
        if not self.enabled:
            logger.info("批量推理未启用（BATCH_ENABLED=false），合成将逐条执行。")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="batch-worker", daemon=True
        )
        self._thread.start()
        logger.info(
            "批量推理调度器已启动：单批上限=%d 条，等待窗口=%.0f 毫秒",
            self.max_size,
            self.wait_ms,
        )

    def stop(self) -> None:
        """停止 worker 线程。"""
        self._stop.set()
        # 唤醒仍在队列里等待的请求：否则它们要一直挂到超时才返回，
        # 会把服务关闭过程拖住（默认超时是 300 秒）
        pending: List[BatchItem] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except Empty:
                break
        for item in pending:
            self._wake(item, error=RuntimeError("服务正在关闭，该请求未被执行"))
        if pending:
            logger.warning("服务关闭时有 %d 条请求未执行，已快速失败返回", len(pending))
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 提交（同步 / 异步）
    # ------------------------------------------------------------------
    def submit(self, item: BatchItem) -> Any:
        """同步提交，阻塞直到拿到本条结果。"""
        if not self._use_batch():
            return self._run_single(item)

        item._event = threading.Event()
        started = time.perf_counter()
        self._queue.put(item)
        if not item._event.wait(timeout=self.timeout):
            raise TimeoutError(
                f"等待批量推理结果超时（{self.timeout:.0f} 秒），"
                f"请降低并发或调大 SLOT_WAIT_TIMEOUT。"
            )
        item.wait_ms = (time.perf_counter() - started) * 1000.0
        if item.error is not None:
            raise item.error
        return item.result

    async def submit_async(self, item: BatchItem) -> Any:
        """异步提交，等待期间不占用事件循环。"""
        if not self._use_batch():
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._run_single, item)

        loop = asyncio.get_event_loop()
        item._future = loop.create_future()
        item._loop = loop
        started = time.perf_counter()
        self._queue.put(item)
        try:
            result = await asyncio.wait_for(item._future, timeout=self.timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"等待批量推理结果超时（{self.timeout:.0f} 秒），"
                f"请降低并发或调大 SLOT_WAIT_TIMEOUT。"
            )
        item.wait_ms = (time.perf_counter() - started) * 1000.0
        return result

    def _use_batch(self) -> bool:
        return self.enabled and self.is_running

    def _run_single(self, item: BatchItem) -> Any:
        """调度器不可用时的降级路径：直接单条推理。"""
        return self.engine.synthesize(
            text=item.text,
            prompt=item.prompt,
            ref_text=None,
            language=item.language or None,
            instruct=item.instruct or None,
            duration=item.duration or None,
            speed=item.speed or None,
            num_step=item.num_step,
            guidance_scale=item.guidance_scale,
            denoise=item.denoise,
            preprocess_prompt=item.preprocess_prompt,
            postprocess_output=item.postprocess_output,
        )

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                batch = self._collect()
                if not batch:
                    continue
                self._run(batch)
            except BaseException as exc:
                # 兜底：调度线程一旦退出，后续所有请求都要等到超时才返回，
                # 因此这里捕获一切异常，保证调度线程持续可用
                logger.error(
                    "批量调度线程出现异常，已跳过本轮继续运行：%s", exc, exc_info=True
                )

    def _collect(self) -> List[BatchItem]:
        """收集一批请求：阻塞等第一条，随后在等待窗口内继续攒。"""
        try:
            first = self._queue.get(timeout=0.2)
        except Empty:
            return []
        batch: List[BatchItem] = [first]

        if self.wait_ms <= 0:
            # 不等待窗口：把当前队列里的请求一次性取完
            while len(batch) < self.max_size:
                try:
                    batch.append(self._queue.get_nowait())
                except Empty:
                    break
            return batch

        deadline = time.monotonic() + self.wait_ms / 1000.0
        while len(batch) < self.max_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(self._queue.get(timeout=remaining))
            except Empty:
                break
        return batch

    def _run(self, batch: List[BatchItem]) -> None:
        """执行一批：按 generation 参数分组，逐组推理。"""
        groups: Dict[Tuple, List[BatchItem]] = {}
        for item in batch:
            groups.setdefault(item.group_key(), []).append(item)

        with self._lock:
            self._batches += len(groups)
            self._items += len(batch)
            self._peak = max(self._peak, max(len(g) for g in groups.values()))

        for key, items in groups.items():
            try:
                self._run_group(items)
            except Exception as exc:  # 整批失败：把异常回传给组内每条请求
                logger.error("批量推理失败（本批 %d 条）：%s", len(items), exc)
                for item in items:
                    self._wake(item, error=exc)

    def _run_group(self, items: List[BatchItem]) -> None:
        has_prompt = items[0].prompt is not None
        if len(items) == 1 or not has_prompt:
            # 单条，或声音设计模式（无参考特征）：走原来的单条路径
            for item in items:
                try:
                    self._wake(item, result=self._run_single(item))
                except Exception as exc:
                    self._wake(item, error=exc)
            return

        first = items[0]
        audios = self.engine.synthesize_batch(
            items=items,
            num_step=first.num_step,
            guidance_scale=first.guidance_scale,
            denoise=first.denoise,
            preprocess_prompt=first.preprocess_prompt,
            postprocess_output=first.postprocess_output,
        )
        if len(audios) != len(items):
            raise RuntimeError(
                f"批量推理返回条数不匹配：期望 {len(items)} 条，实际 {len(audios)} 条"
            )
        for item, waveform in zip(items, audios):
            self._wake(item, result=waveform, batch_size=len(items))

    # ------------------------------------------------------------------
    # 结果回传
    # ------------------------------------------------------------------
    def _wake(
        self,
        item: BatchItem,
        result: Any = None,
        error: Optional[BaseException] = None,
        batch_size: int = 0,
    ) -> None:
        if error is not None:
            item.error = error
        else:
            item.result = result
        if batch_size:
            item.batch_size = batch_size
        if item._event is not None:
            item._event.set()
        if item._future is not None and item._loop is not None:
            item._loop.call_soon_threadsafe(self._resolve_future, item)

    @staticmethod
    def _resolve_future(item: BatchItem) -> None:
        """在事件循环线程里兑现 Future（worker 线程不能直接操作）。"""
        if item._future is None or item._future.done():
            return
        if item.error is not None:
            item._future.set_exception(item.error)
        else:
            item._future.set_result(item.result)

    # ------------------------------------------------------------------
    # 运行状态
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            batches, items, peak = self._batches, self._items, self._peak
        return {
            "批量推理": "已启用" if self.enabled else "未启用",
            "调度线程": "运行中" if self.is_running else "未运行",
            "单批上限": self.max_size,
            "等待窗口(毫秒)": round(self.wait_ms, 1),
            "队列积压": self._queue.qsize(),
            "已处理批次数": batches,
            "已处理条数": items,
            "历史最大批量": peak,
            "平均批量": round(items / batches, 2) if batches else 0.0,
        }


_scheduler: Optional[BatchScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler(engine: Any = None) -> BatchScheduler:
    """获取全局唯一的批量调度器。"""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                from .config import get_settings
                from .engine import get_engine

                settings = get_settings()
                _scheduler = BatchScheduler(
                    engine=engine or get_engine(settings),
                    max_size=settings.batch_max_size,
                    wait_ms=settings.batch_wait_ms,
                    enabled=settings.batch_enabled,
                    timeout=settings.slot_wait_timeout,
                )
    return _scheduler
