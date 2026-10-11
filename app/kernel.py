"""模块内核：每个模块回答一个问题，每 10 分钟运行一次，然后用独立来源交叉核对结论。

· Module：title / question（回答什么）/ params（关键参数的当前值）/ tick（周期任务）/ checks（交叉校验）。
· 模块可在后台单独开关（设置 module_<名字>，或用 get_enabled / set_enabled 对接已有设置）。
  关闭只停止自动调整，已生效的数值保持；保护类硬限制（每小时上限等）不随开关失效。
· 每轮把运行结果记进 kernel_last，并保留最近 RUNS 轮（后台画运行条）。
· 校验不通过记进 Bug 追踪；level 为 warn 的校验只在后台标黄，不记 Bug（风险检查用）。
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

STATE_KEY = "kernel_last"
RUNS = 36                       # 每个模块保留最近 36 轮（6 小时）


@dataclass
class Param:
    name: str
    value: Callable[[], Any]          # 读当前生效值
    note: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    level: str = ""                   # 空 = 按 ok 判断；ok / warn / bad / na（风险检查用）
    evidence: list[str] = field(default_factory=list)


@dataclass
class Module:
    name: str
    title: str
    question: str
    default_enabled: bool = True
    params: list[Param] = field(default_factory=list)
    tick: Optional[Callable[["Kernel"], Any]] = None
    checks: Optional[Callable[["Kernel"], Any]] = None
    get_enabled: Optional[Callable[["Kernel"], Any]] = None
    set_enabled: Optional[Callable[["Kernel", bool], Any]] = None
    hard_note: str = ""               # 关闭后还有什么仍然生效
    report_bugs: bool = True          # 校验失败时记进 Bug 追踪


class Kernel:
    def __init__(self, state, bug: Callable[..., None] = lambda *a, **k: None):
        self.state, self.db, self.bug = state, state.db, bug
        self.modules: dict[str, Module] = {}
        self.last: dict[str, dict] = {}
        self.extra: dict[str, Any] = {}       # 模块需要的外部对象（registrar、通知函数等）

    def register(self, module: Module) -> None:
        self.modules[module.name] = module

    async def load(self) -> None:
        """启动时读回上次的运行记录，运行条不会因为重启清空。"""
        try:
            self.last = json.loads(await self.db.get_setting(STATE_KEY, "{}") or "{}")
        except (TypeError, ValueError):
            self.last = {}

    async def enabled(self, name: str) -> bool:
        m = self.modules[name]
        if m.get_enabled is not None:
            return bool(await m.get_enabled(self))
        raw = await self.db.get_setting("module_" + name, None)
        return m.default_enabled if raw is None else str(raw) == "1"

    async def set_enabled(self, name: str, on: bool) -> None:
        m = self.modules[name]
        if m.set_enabled is not None:
            await m.set_enabled(self, on)
        else:
            await self.db.set_setting("module_" + name, "1" if on else "0")

    async def tick_all(self) -> dict[str, dict]:
        """按登记顺序运行启用的模块，再做交叉校验；每个模块记一轮运行结果。单个模块出错不影响其他模块。"""
        now = time.time()
        for name, m in self.modules.items():
            entry = self.last.setdefault(name, {})
            on = await self.enabled(name)
            entry["enabled"] = on
            entry.pop("checks", None)
            if not on or m.tick is None:
                continue
            t0 = time.time()
            try:
                result = await m.tick(self)
                entry.update(at=t0, ok=True, ms=int((time.time() - t0) * 1000),
                             summary=str(result)[:300] if result is not None else "", error="")
            except Exception as exc:
                entry.update(at=t0, ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
                self.bug("module:" + name, exc)
        await self.check_all()
        for name in self.modules:
            entry = self.last[name]
            if not entry.get("enabled"):
                continue
            checks = entry.get("checks") or []
            level = ("bad" if entry.get("ok") is False or any(c["level"] == "bad" for c in checks)
                     else "warn" if any(c["level"] == "warn" for c in checks) else "ok")
            entry["runs"] = (entry.get("runs") or [])[-(RUNS - 1):] + [{"at": now, "level": level}]
        await self.db.set_setting(STATE_KEY, json.dumps(self.last, ensure_ascii=False, default=str))
        return self.last

    async def check_all(self) -> list[tuple[str, Check]]:
        bad: list[tuple[str, Check]] = []
        for name, m in self.modules.items():
            if m.checks is None or not await self.enabled(name):
                continue
            try:
                checks = list(await m.checks(self))
            except Exception as exc:
                checks = [Check("校验本身出错", False, f"{type(exc).__name__}: {exc}"[:200])]
                traceback.print_exc()
            for c in checks:
                c.level = c.level or ("ok" if c.ok else "bad")
            self.last.setdefault(name, {})["checks"] = [c.__dict__ for c in checks]
            for c in checks:
                if not c.ok:
                    bad.append((name, c))
                    if m.report_bugs:
                        self.bug(f"check:{name}:{c.name}", title=f"交叉校验不一致：{m.title} · {c.name}：{c.detail}"[:200],
                                 level="warn")
        return bad

    async def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for name, m in self.modules.items():
            params = []
            for p in m.params:
                try:
                    v = p.value()
                    v = await v if hasattr(v, "__await__") else v
                except Exception as exc:
                    v = f"读取失败：{exc}"
                params.append({"name": p.name, "value": v, "note": p.note})
            out.append({"name": name, "title": m.title, "question": m.question, "enabled": await self.enabled(name),
                        "hard_note": m.hard_note, "periodic": m.tick is not None, "last": self.last.get(name, {}),
                        "params": params})
        return out
