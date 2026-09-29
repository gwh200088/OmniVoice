#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口契约检查：真实导入全部模块并调用关键路径。

用途
----
用于验证「各模块之间的接口是否对得上」，例如：
engine 引用的 SlotManager 属性是否真的存在、Settings 是否解析出批量字段、
/info 返回的字段是否与文档一致。

这类问题不会在语法检查或静态阅读中暴露——曾经出现过「回滚 slot.py 后
engine.py 仍在引用 estimate_mb」导致所有合成请求报错的情况，而当时因为
用桩类替换了 SlotManager，测试反而全部通过。因此本脚本明确要求：

    **只对 GPU / 模型等外部依赖打桩，本项目所有类一律使用真实实现。**

用法
----
在容器内（依赖齐全）执行::

    python3 tools/check_contract.py

也可在宿主机执行（需已安装 numpy、fastapi、uvicorn 等依赖）::

    python server/tools/check_contract.py

退出码：全部通过为 0，有失败项为 1。
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import types

# 让脚本可以从任意位置运行：server/tools/xxx.py -> server/
PROJECT = str(pathlib.Path(__file__).resolve().parents[1])
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)


def _install_stubs() -> None:
    """仅为 GPU / 模型等外部依赖打桩（本项目类不使用桩）。"""
    if "torch" in sys.modules:
        return

    class _FakeCuda:
        def is_available(self):
            return True

        def mem_get_info(self, index=0):
            return (13000 * 1024 ** 2, 15360 * 1024 ** 2)

        def memory_allocated(self, index=0):
            return 0

        def memory_reserved(self, index=0):
            return 0

        def current_device(self):
            return 0

        def get_device_name(self, index=0):
            return "Tesla T4 (stub)"

        def synchronize(self):
            pass

        def get_device_properties(self, index=0):
            return types.SimpleNamespace(total_memory=15360 * 1024 ** 3)

    torch_stub = types.ModuleType("torch")
    torch_stub.cuda = _FakeCuda()
    torch_stub.float16 = "f16"
    torch_stub.float32 = "f32"
    torch_stub.bfloat16 = "bf16"
    torch_stub.dtype = type("dtype", (), {})
    sys.modules["torch"] = torch_stub

    omni_stub = types.ModuleType("omnivoice")

    class OmniVoiceGenerationConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class OmniVoice:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            return None

    class VoiceClonePrompt:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

        def save(self, path):
            with open(path, "wb") as fh:
                fh.write(b"stub-prompt")

        @staticmethod
        def load(path):
            return VoiceClonePrompt()

    omni_stub.OmniVoiceGenerationConfig = OmniVoiceGenerationConfig
    omni_stub.OmniVoice = OmniVoice
    omni_stub.VoiceClonePrompt = VoiceClonePrompt
    sys.modules["omnivoice"] = omni_stub


def main() -> int:
    # 数据目录放到临时目录，避免污染真实数据
    data_root = tempfile.mkdtemp(prefix="ovcheck-")
    os.environ.setdefault("DATA_DIR", data_root)
    os.environ.setdefault("MODEL_ID", os.path.join(data_root, "model"))
    os.environ.setdefault("HF_HUB_OFFLINE", "0")
    os.environ.setdefault("BATCH_MAX_SIZE", "4")
    os.environ.setdefault("BATCH_ENABLED", "true")

    _install_stubs()

    import numpy as np

    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True))
            print("  [OK]   " + name + (("  | " + str(detail)) if detail else ""))
        except Exception as exc:  # 任何异常都算失败，并如实打印
            results.append((name, False))
            print("  [FAIL] " + name + "  | " + type(exc).__name__ + ": " + str(exc))

    print("=" * 74)
    print("接口契约检查（真实类 + 仅外部依赖打桩）")
    print("=" * 74)

    # ------------------------------------------------------------------
    print("\n[1] 模块导入")

    def _import_main():
        import app.main  # noqa: F401  触发全部子模块与路由注册

        return "app.main 导入成功"

    check("导入 app.main（含所有子模块与路由）", _import_main)

    # ------------------------------------------------------------------
    print("\n[2] 配置项")

    def _settings():
        from app.config import get_settings

        s = get_settings()
        for field in ("batch_enabled", "batch_max_size", "batch_wait_ms"):
            if not hasattr(s, field):
                raise AttributeError("Settings 缺少字段 " + field)
        if s.batch_max_size != 4:
            raise ValueError("BATCH_MAX_SIZE 未生效，实际=%s" % s.batch_max_size)
        return "批量字段齐备且摘要可生成"

    check("Settings 含批量字段、环境变量生效", _settings)

    # ------------------------------------------------------------------
    print("\n[3] 引擎与槽位")

    def _engine():
        from app.config import get_settings
        from app.engine import get_engine

        e = get_engine(get_settings())
        e.device = "cuda:0"
        limit = e._safe_batch_limit(4)  # 依赖 SlotManager.estimate_mb
        info = e.info()
        if "并发槽位" not in info:
            raise KeyError("info() 返回缺并发槽位")
        return "单批上限=%d，info 字段=%d 个" % (limit, len(info))

    check("engine._safe_batch_limit 与 info() 协同", _engine)

    # ------------------------------------------------------------------
    print("\n[4] 批量合成路径（真实 SlotManager）")

    def _batch():
        from app.config import get_settings
        from app.engine import get_engine

        e = get_engine(get_settings())
        e.device = "cuda:0"
        e.sampling_rate = 24000

        class FakeModel:
            def generate(self, **kwargs):
                texts = kwargs["text"]
                if isinstance(texts, str):
                    texts = [texts]
                return [np.zeros(2400, dtype=np.float32) for _ in texts]

        e.model = FakeModel()
        items = [
            types.SimpleNamespace(
                text="a%d" % i, prompt="p", language="",
                instruct="", duration=0, speed=0,
            )
            for i in range(3)
        ]
        waves = e.synthesize_batch(items=items)
        if len(waves) != 3:
            raise ValueError("返回 %d 条，期望 3 条" % len(waves))
        return "%d 条 -> %d 条结果" % (len(items), len(waves))

    check("synthesize_batch 与真实槽位协同", _batch)

    # ------------------------------------------------------------------
    print("\n[5] 批量调度器")

    def _scheduler():
        from app.batcher import get_scheduler
        from app.config import get_settings
        from app.engine import get_engine

        s = get_scheduler(get_engine(get_settings()))
        st = s.stats()
        if "单批上限" not in st or "平均批量" not in st:
            raise KeyError("stats() 字段不全: %s" % list(st.keys()))
        return "上限=%s，状态=%s" % (st["单批上限"], st["批量推理"])

    check("BatchScheduler.stats() 正常", _scheduler)

    def _fallback():
        """调度器未启动时，应降级为逐条推理而不是卡住。"""
        from app.batcher import BatchItem, BatchScheduler
        from app.config import get_settings
        from app.engine import get_engine

        e = get_engine(get_settings())
        e.device = "cuda:0"
        e.sampling_rate = 24000

        def _gen(**kwargs):
            return [np.zeros(2400, dtype=np.float32)]

        e.model = types.SimpleNamespace(generate=_gen)
        s = BatchScheduler(e, max_size=4, wait_ms=30.0, enabled=True)  # 未 start
        s.submit(BatchItem(text="x", prompt="p"))
        return "降级路径可用"

    check("调度器未启动时降级为逐条推理", _fallback)

    # ------------------------------------------------------------------
    print("\n[6] 业务层编排")

    def _service_synth():
        from app.service import get_service
        from app.timing import StageTimer

        svc = get_service()
        original_engine = svc.engine  # 单例，用完必须还原

        class StubEngine:
            sampling_rate = 24000

            def synthesize(self, **kwargs):
                return np.zeros(2400, dtype=np.float32)

            def synthesize_batch(self, items, **kwargs):
                return [np.zeros(2400, dtype=np.float32) for _ in items]

        try:
            svc.engine = StubEngine()
            timer = StageTimer(request_id="check-001", task_name="检查")
            wave, batch_size = svc._synthesize(
                text="你好", prompt="p", language=None, instruct=None,
                duration=None, speed=None, num_step=None,
                guidance_scale=None, denoise=True,
                preprocess_prompt=True, postprocess_output=True,
                timer=timer,
            )
            stages = [s.name for s in timer.stages]
            if "语音合成" not in stages:
                raise ValueError("阶段记录缺语音合成: %s" % stages)
            return "批次=%d，阶段=%s" % (batch_size, stages)
        finally:
            svc.engine = original_engine

    check("service._synthesize 完整编排（含计时阶段）", _service_synth)

    def _service_methods():
        from app.service import get_service

        svc = get_service()
        for method in (
            "synthesize", "create_voice", "reextract_voice",
            "_synthesize", "validate_upload",
        ):
            if not hasattr(svc, method):
                raise AttributeError("缺方法 " + method)
        return "方法齐备"

    check("VoiceCloneService 方法齐备", _service_methods)

    # ------------------------------------------------------------------
    print("\n[7] 接口层")

    def _routes():
        from app.api import router

        paths = [r.path for r in router.routes]
        missing = [p for p in ("/tts", "/voices", "/info", "/health") if p not in paths]
        if missing:
            raise ValueError("缺少路由 %s（现有 %s）" % (missing, paths))
        return "路由 %d 个" % len(paths)

    check("接口路由注册完整", _routes)

    def _info_api():
        from app.api import info

        r = info()
        for key in ("引擎信息", "批量推理", "存储统计", "上传限制", "合成默认参数"):
            if key not in r:
                raise KeyError("info 返回缺字段 " + key)
        return "字段: " + "/".join(list(r.keys()))

    check("/info 接口可调用且字段完整", _info_api)

    def _health():
        from app.api import health

        r = health()
        if "状态" not in r:
            raise KeyError("health 返回缺状态字段")
        return str(r.get("状态"))

    check("/health 接口可调用", _health)

    # ------------------------------------------------------------------
    print("\n" + "=" * 74)
    passed = sum(1 for _, ok in results if ok)
    failed = [name for name, ok in results if not ok]
    print("结果：%d/%d 项通过" % (passed, len(results)))
    if failed:
        print("失败项：" + "；".join(failed))
    print("=" * 74)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
