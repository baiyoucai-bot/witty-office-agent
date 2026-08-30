"""可逆副作用栈：装上登记清理函数，卸下倒序执行。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from witty_agent.logging import get_logger

logger = get_logger("plugins")

Dispose = Callable[[], None]


@dataclass
class Effect:
    label: str
    dispose: Dispose


@dataclass
class EffectStack:
    _scopes: dict[str, list[Effect]] = field(default_factory=dict)

    def push(self, scope: str, dispose: Dispose, label: str = "") -> None:
        self._scopes.setdefault(scope, []).append(Effect(label=label or scope, dispose=dispose))
        logger.info("登记副作用 scope=%s label=%s", scope, label or scope)

    def unwind(self, scope: str) -> int:
        effects = list(reversed(self._scopes.pop(scope, [])))
        done = 0
        for item in effects:
            try:
                item.dispose()
                done += 1
            except Exception as exc:
                logger.warning("回滚失败 scope=%s label=%s err=%s", scope, item.label, exc)
        if effects:
            logger.info("回滚副作用 scope=%s count=%s", scope, done)
        return done

    def unwind_all(self) -> int:
        total = 0
        for scope in reversed(list(self._scopes)):
            total += self.unwind(scope)
        return total

    def scopes(self) -> list[str]:
        return list(self._scopes)

    def clear(self) -> None:
        self._scopes.clear()


STACK = EffectStack()
