"""斜杠命令不经模型轮次，直接分派。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from witty_agent.kernel_surface import KERNEL_COMMANDS, is_kernel_command
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt

logger = get_logger("commands")


@dataclass
class CommandResult:
    kind: str
    text: str = ""
    remainder: str = ""


CommandHandler = Callable[[str], CommandResult]


@dataclass
class CommandSpec:
    name: str
    description: str
    handler: CommandHandler


@dataclass
class CommandRegistry:
    _items: dict[str, CommandSpec] = field(default_factory=dict)

    def register(self, name: str, description: str, handler: CommandHandler) -> None:
        existing = self._items.get(name)
        if existing is not None and is_kernel_command(name):
            logger.warning("拒绝覆盖内核命令 name=%s", name)
            return
        self._items[name] = CommandSpec(name=name, description=description, handler=handler)

    def unregister(self, name: str) -> None:
        if is_kernel_command(name):
            raise ValueError(f"内核命令不可卸载: /{name}")
        self._items.pop(name, None)

    def get(self, name: str) -> CommandSpec | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)

    def kernel_names(self) -> frozenset[str]:
        return KERNEL_COMMANDS

    def public_items(self) -> list[dict[str, object]]:
        return [
            {
                "name": name,
                "description": self._items[name].description,
                "kernel": is_kernel_command(name),
            }
            for name in self.names()
        ]

    @staticmethod
    def kernel_catalog() -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for name in sorted(KERNEL_COMMANDS):
            try:
                desc = get_prompt(f"command_desc_{name}")
            except KeyError:
                desc = name
            items.append({"name": name, "description": desc, "kernel": True})
        return items

    def parse(self, raw: str) -> tuple[str, str] | None:
        text = raw.strip()
        if not text.startswith("/"):
            return None
        first = text.split(None, 1)
        token = first[0][1:]
        if not token or token not in self._items:
            return None
        rest = first[1] if len(first) > 1 else ""
        return token, rest

    def dispatch(self, raw: str) -> CommandResult | None:
        parsed = self.parse(raw)
        if parsed is None:
            return None
        name, rest = parsed
        spec = self._items[name]
        logger.info("分派命令 name=%s", name)
        return spec.handler(rest)

    def catalog_text(self) -> str:
        if not self._items:
            return ""
        lines = [get_prompt("commands_intro")]
        for name in self.names():
            spec = self._items[name]
            lines.append(f"- /{name}: {spec.description}")
        return "\n".join(lines) + "\n"
