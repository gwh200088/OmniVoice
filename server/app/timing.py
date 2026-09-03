"""阶段耗时统计。

整体流程被拆成若干阶段（音频转码、特征提取、语音合成、音频编码等），
每个阶段单独计时并打印中文日志，便于快速定位性能瓶颈。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from .logging_setup import get_logger


def new_request_id(prefix: str = "req") -> str:
    """生成一个简短的请求编号，用于串联同一请求的所有日志。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class Stage:
    """单个阶段的耗时记录。"""

    name: str
    elapsed_ms: float = 0.0
    note: str = ""

    def __str__(self) -> str:
        base = f"{self.name}={self.elapsed_ms:.1f}毫秒"
        if self.note:
            base = f"{base}（{self.note}）"
        return base


@dataclass
class StageTimer:
    """请求级计时器，按顺序记录各阶段耗时。"""

    request_id: str
    task_name: str = "任务"
    logger_name: str = ""
    stages: List[Stage] = field(default_factory=list)
    _start: float = field(default_factory=time.perf_counter)
    _logger: Optional[object] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._logger = get_logger(self.logger_name)

    @property
    def total_ms(self) -> float:
        """从计时器创建到目前为止的总耗时。"""
        return (time.perf_counter() - self._start) * 1000.0

    @property
    def sum_ms(self) -> float:
        """所有已记录阶段耗时的总和。"""
        return sum(stage.elapsed_ms for stage in self.stages)

    @contextmanager
    def stage(self, name: str, note: str = "") -> Iterator[None]:
        """用 `with timer.stage("特征提取"):` 的形式为一段代码计时。"""
        started = time.perf_counter()
        self._logger.info("[%s] 阶段【%s】开始 ...", self.request_id, name)
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.stages.append(Stage(name=name, elapsed_ms=elapsed, note=note))
            extra = f"，备注：{note}" if note else ""
            self._logger.info(
                "[%s] 阶段【%s】结束，耗时 %.1f 毫秒%s",
                self.request_id,
                name,
                elapsed,
                extra,
            )

    def record(self, name: str, elapsed_ms: float, note: str = "") -> None:
        """直接记录一个阶段的耗时（用于无法用上下文包裹的场景）。"""
        self.stages.append(Stage(name=name, elapsed_ms=elapsed_ms, note=note))
        self._logger.info(
            "[%s] 阶段【%s】耗时 %.1f 毫秒", self.request_id, name, elapsed_ms
        )

    def last_stage_ms(self, name: Optional[str] = None) -> float:
        """取最近一个阶段（或指定名称的最近一个阶段）的耗时。"""
        for stage in reversed(self.stages):
            if name is None or stage.name == name:
                return stage.elapsed_ms
        return 0.0

    def summary(self, log: bool = True) -> str:
        """输出总耗时与阶段明细，返回汇总字符串。"""
        total = self.total_ms
        detail = "、".join(str(stage) for stage in self.stages) or "无阶段记录"
        text = (
            f"[{self.request_id}] {self.task_name}全部完成，"
            f"总耗时 {total:.1f} 毫秒；阶段明细：{detail}"
        )
        if log:
            self._logger.info(text)
        return text

    def as_dict(self) -> Dict[str, float]:
        """以字典形式返回各阶段耗时，便于放入接口响应。"""
        data = {stage.name: round(stage.elapsed_ms, 1) for stage in self.stages}
        data["总耗时"] = round(self.total_ms, 1)
        return data

    def stage_list(self) -> List[Dict[str, object]]:
        """以列表形式返回阶段明细，保持阶段先后顺序。"""
        return [
            {
                "阶段": stage.name,
                "耗时毫秒": round(stage.elapsed_ms, 1),
                "备注": stage.note,
            }
            for stage in self.stages
        ]
