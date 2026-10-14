"""对局闸门 —— 强制暂停 / 单步执行，用于调试。

挂在引擎的每一次模型调用之前（``Game._ask``）。被暂停时，对局线程会阻塞在
这里，不会发出任何请求，也不会消耗额度；继续之后从原处接着跑。

三种状态：
  · 运行中   —— ``paused=False``，wait() 直接放行
  · 已暂停   —— ``paused=True``，wait() 阻塞，页面上点「继续」才放行
  · 单步中   —— ``paused=True`` 且 ``steps>0``，放行 steps 次后重新阻塞

线程安全：对局线程阻塞等待，HTTP 线程负责 pause/resume/step。
"""
from __future__ import annotations

import threading
from typing import Callable, Dict, Optional


class GameAborted(Exception):
    """对局被强制结束。由闸门在 wait() 里抛出。"""


class GameGate:
    def __init__(self, on_state: Optional[Callable[[dict], None]] = None) -> None:
        self._cond = threading.Condition()   # 默认是 RLock，_emit 里重入安全
        self._paused = False
        self._steps = 0
        self._label = ""
        self._waiting = 0
        self._aborted = False
        self.on_state = on_state

    # ---------------- 查询 ----------------

    def snapshot(self) -> Dict:
        with self._cond:
            return {
                "paused": self._paused,
                "steps_left": self._steps,
                "label": self._label,
                "waiting": self._waiting,
                "aborted": self._aborted,
            }

    @property
    def paused(self) -> bool:
        with self._cond:
            return self._paused

    @property
    def aborted(self) -> bool:
        with self._cond:
            return self._aborted

    # ---------------- 控制 ----------------

    def pause(self, label: str = "") -> Dict:
        with self._cond:
            self._paused = True
            self._steps = 0
            if label:
                self._label = label
            self._cond.notify_all()
        self._emit("pause")
        return self.snapshot()

    def resume(self) -> Dict:
        with self._cond:
            self._paused = False
            self._steps = 0
            self._aborted = False
            self._cond.notify_all()
        self._emit("resume")
        return self.snapshot()

    def step(self, n: int = 1) -> Dict:
        """放行 n 次调用，然后重新阻塞。"""
        with self._cond:
            self._paused = True
            self._steps += max(1, int(n))
            self._cond.notify_all()
        self._emit("step")
        return self.snapshot()

    def toggle(self) -> Dict:
        return self.resume() if self.paused else self.pause()

    def abort(self) -> Dict:
        """强制结束当前对局：唤醒所有等待者，并让它们抛 GameAborted。

        注意：如果对局线程此刻正卡在一个飞行中的 HTTP 请求里，
        要等那个请求返回才会真正退出——闸门拦不住已经在飞的请求。
        """
        with self._cond:
            self._aborted = True
            self._paused = False
            self._steps = 0
            self._cond.notify_all()
        self._emit("abort")
        return self.snapshot()

    # ---------------- 对局线程入口 ----------------

    def wait(self, label: str = "") -> None:
        """在每次模型调用前调用。被暂停时阻塞在这里；被强制结束则抛异常。"""
        with self._cond:
            if self._aborted:
                raise GameAborted()
            if label:
                self._label = label
            if not self._paused:
                return
            if self._steps > 0:
                self._steps -= 1
                self._emit("step_consumed")
                return

            self._waiting += 1
            self._emit("blocked")
            try:
                while self._paused and self._steps <= 0 and not self._aborted:
                    self._cond.wait(timeout=0.5)
                if self._aborted:
                    raise GameAborted()
                if self._steps > 0:
                    self._steps -= 1
            finally:
                self._waiting -= 1
            self._emit("released")

    # ---------------- 内部 ----------------

    def _emit(self, event: str) -> None:
        if not self.on_state:
            return
        try:
            self.on_state({"type": "gate", "event": event, "gate": self.snapshot()})
        except Exception:
            pass   # 推流失败绝不能影响对局


class ConsoleGate(GameGate):
    """命令行用：每次调用前停下来等回车。"""

    def __init__(self, show=None) -> None:
        super().__init__()
        self._show = show or (lambda label: None)

    def wait(self, label: str = "") -> None:
        self._show(label)
        try:
            input("    ⏸  回车继续…")
        except EOFError:
            pass
        except KeyboardInterrupt:
            raise GameAborted()
