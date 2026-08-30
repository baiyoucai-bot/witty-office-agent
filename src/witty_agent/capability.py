"""会话里的具名能力表，不是插件内核。

循环、内置工具和斜杠命令不走这里，也不能从这里卸载。
问答提供方等可选实现可以 provide，替换的是实现，不是内核契约。
"""

from __future__ import annotations

from typing import Any

from witty_agent.logging import get_logger

logger = get_logger("capability")


class CapabilityRegistry:
    """进程内具名能力表。不是插件内核，卸载即删。"""

    def __init__(self) -> None:
        self._providers: dict[str, Any] = {}

    def provide(self, name: str, instance: Any) -> None:
        self._providers[name] = instance
        logger.info("能力提供 name=%s type=%s", name, type(instance).__name__)

    def get(self, name: str) -> Any:
        try:
            return self._providers[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._providers)) or "(空)"
            raise KeyError(f"未注册能力 {name!r}，已有: {known}") from exc

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)
