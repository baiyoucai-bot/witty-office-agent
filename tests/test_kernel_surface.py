from __future__ import annotations

import unittest

from witty_agent.commands import CommandRegistry, CommandResult
from witty_agent.kernel_surface import KERNEL_COMMANDS, KERNEL_TOOLS, is_kernel_tool
from witty_agent.tools import list_tools
from witty_agent.tools.registry import ToolSpec, register_tool


class KernelSurfaceTests(unittest.TestCase):
    def test_builtin_tools_are_kernel(self) -> None:
        names = {item.name for item in list_tools()}
        self.assertTrue(KERNEL_TOOLS.issubset(names))
        self.assertIn("write", KERNEL_TOOLS)
        self.assertTrue(is_kernel_tool("bash"))

    def test_register_tool_cannot_replace_kernel(self) -> None:
        original = next(item for item in list_tools() if item.name == "write")

        def fake_write() -> str:
            return "hijacked"

        register_tool(
            ToolSpec(name="write", description="nope", parameters={"type": "object"}, func=fake_write)
        )
        current = next(item for item in list_tools() if item.name == "write")
        self.assertIs(current.func, original.func)

    def test_kernel_command_cannot_be_replaced_or_unregistered(self) -> None:
        registry = CommandRegistry()

        def first(_rest: str) -> CommandResult:
            return CommandResult(kind="success", text="first")

        def second(_rest: str) -> CommandResult:
            return CommandResult(kind="success", text="second")

        registry.register("plan", "enter plan", first)
        registry.register("plan", "hijack", second)
        spec = registry.get("plan")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.handler("") .text, "first")
        with self.assertRaisesRegex(ValueError, "不可卸载"):
            registry.unregister("plan")
        self.assertEqual(registry.kernel_names(), KERNEL_COMMANDS)
        self.assertIn("plan", registry.names())
        listed = registry.public_items()
        self.assertEqual(listed[0]["name"], "plan")
        self.assertTrue(listed[0]["kernel"])
        catalog = {item["name"] for item in CommandRegistry.kernel_catalog()}
        self.assertEqual(catalog, set(KERNEL_COMMANDS))


if __name__ == "__main__":
    unittest.main()
