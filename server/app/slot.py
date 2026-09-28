"""推理槽位管理：按显存余量自适应控制并发。

工作方式：

- 每次推理前先申请一个"槽位"；
- 若当前并发数未达上限**且**显存余量充足（扣除安全预留后仍够一次推理），立即放行；
- 否则进入等待队列，直到有推理完成、释放槽位后再尝试；
- 单次推理的显存占用会自适应学习，避免手工估算不准。

CPU 模式下没有显存概念，仅按并发上限控制。
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from .logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class SlotLease:
    """一次推理的槽位租约，记录等待与显存信息。"""

    wait_ms: float = 0.0
    free_before_mb: Optional[float] = None


def _device_index(device: str) -> int:
    """从设备字符串解析显卡编号。"""
    if ":" in device:
        try:
            return int(device.split(":")[-1])
        except (TypeError, ValueError):
            return 0
    return 0


class SlotManager:
    """推理槽位管理器。"""

    def __init__(
        self,
        max_concurrency: int = 2,
        slot_memory_mb: float = 0.0,
        reserve_mb: float = 512.0,
        wait_timeout: float = 300.0,
        device: str = "",
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.reserve_mb = max(0.0, float(reserve_mb))
        self.wait_timeout = max(1.0, float(wait_timeout))
        self.device = device or ""

        # 单次推理显存估算：0 表示自适应学习
        self._auto_estimate = float(slot_memory_mb) <= 0
        self._estimate_mb = float(slot_memory_mb) if slot_memory_mb > 0 else 2048.0

        self._condition = threading.Condition()
        self._active = 0          # 正在推理的数量
        self._waiting = 0         # 正在等待槽位的数量
        self._peak_active = 0     # 历史最大并发数

        if self._auto_estimate:
            total = self.total_memory_mb()
            if total and total > 0:
                # 初始按总显存的 15% 估算，后续自适应修正
                self._estimate_mb = max(512.0, total * 0.15)
                logger.info(
                    "推理槽位：自动模式，初始按 %.0f MB / 卡（总显存 %.0f MB）估算单次推理占用",
                    self._estimate_mb,
                    total,
                )

    # ------------------------------------------------------------------
    # 显存查询
    # ------------------------------------------------------------------
    @property
    def is_cuda(self) -> bool:
        return self.device.startswith("cuda")

    def total_memory_mb(self) -> Optional[float]:
        """显卡总显存（MB），非 GPU 环境返回 None。"""
        if not self.is_cuda:
            return None
        try:
            import torch

            if not torch.cuda.is_available():
                return None
            _, total = torch.cuda.mem_get_info(_device_index(self.device))
            return total / 1024**2
        except Exception:
            return None

    def free_memory_mb(self) -> Optional[float]:
        """当前可用显存（MB，设备级），非 GPU 环境返回 None。"""
        if not self.is_cuda:
            return None
        try:
            import torch

            if not torch.cuda.is_available():
                return None
            free, _ = torch.cuda.mem_get_info(_device_index(self.device))
            return free / 1024**2
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 槽位申请与释放
    # ------------------------------------------------------------------
    def _can_start(self) -> bool:
        """判断当前是否还能放行一次推理。"""
        if self._active >= self.max_concurrency:
            return False
        if not self.is_cuda:
            # CPU 模式只受并发上限约束
            return True
        free_mb = self.free_memory_mb()
        if free_mb is None:
            return True
        return (free_mb - self.reserve_mb) >= self._estimate_mb

    def _acquire(self) -> bool:
        """申请槽位，成功返回 True，超时返回 False。"""
        deadline = time.monotonic() + self.wait_timeout
        with self._condition:
            self._waiting += 1
            try:
                while not self._can_start():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "等待推理槽位超时（%.0f 秒），当前并发=%d，等待队列=%d",
                            self.wait_timeout,
                            self._active,
                            self._waiting - 1,
                        )
                        return False
                    # 显存变化不会主动通知，用短超时轮询以便及时感知
                    self._condition.wait(min(0.2, remaining))

                self._active += 1
                self._peak_active = max(self._peak_active, self._active)
                return True
            finally:
                self._waiting -= 1

    def _release(self, lease: SlotLease) -> None:
        """释放槽位，并根据本次实际占用修正显存估算。"""
        if self.is_cuda and lease.free_before_mb is not None:
            free_now = self.free_memory_mb()
            if free_now is not None:
                used = lease.free_before_mb - free_now
                if used > 0:
                    self._update_estimate(used)

        with self._condition:
            self._active -= 1
            self._condition.notify_all()

        logger.info(
            "推理槽位释放：当前并发=%d/%d，等待队列=%d，单次占用估算=%.0f MB",
            self._active,
            self.max_concurrency,
            self._waiting,
            self._estimate_mb,
        )

    def _update_estimate(self, used_mb: float) -> None:
        """自适应修正单次推理的显存估算（指数滑动平均）。"""
        if not self._auto_estimate:
            return
        self._estimate_mb = self._estimate_mb * 0.7 + used_mb * 0.3
        logger.debug("修正单次推理显存估算：%.0f MB", self._estimate_mb)

    @contextmanager
    def slot(self) -> Iterator[SlotLease]:
        """获取一个推理槽位（上下文管理器用法）。

        用法::

            with slots.slot() as lease:
                if lease.wait_ms > 0:
                    ...  # 记录排队耗时
                执行推理
        """
        started = time.perf_counter()
        lease = SlotLease()
        if not self._acquire():
            raise TimeoutError(
                f"等待推理槽位超时（{self.wait_timeout:.0f} 秒），"
                f"请降低并发或调大 MAX_CONCURRENCY。"
            )
        lease.wait_ms = (time.perf_counter() - started) * 1000.0
        lease.free_before_mb = self.free_memory_mb()

        if lease.wait_ms >= 5.0:
            logger.info(
                "请求等待推理槽位 %.1f 毫秒后进入推理（当前并发=%d/%d）",
                lease.wait_ms,
                self._active,
                self.max_concurrency,
            )

        try:
            yield lease
        finally:
            self._release(lease)

    # ------------------------------------------------------------------
    # 运行状态
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """返回槽位运行状态，便于监控与排障。"""
        free_mb = self.free_memory_mb()
        total_mb = self.total_memory_mb()
        data = {
            "最大并发数": self.max_concurrency,
            "当前推理数": self._active,
            "等待队列长度": self._waiting,
            "历史最大并发": self._peak_active,
            "单次推理显存估算(MB)": round(self._estimate_mb, 1),
            "显存估算方式": "自适应学习" if self._auto_estimate else "手动指定",
            "显存安全预留(MB)": round(self.reserve_mb, 1),
        }
        if free_mb is not None:
            data["当前可用显存(MB)"] = round(free_mb, 1)
        if total_mb is not None:
            data["显存总量(MB)"] = round(total_mb, 1)
        return data
