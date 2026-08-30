from __future__ import annotations

import unittest

from witty_agent.llm import ScriptedLLM, text_reply, tool_reply
from witty_agent.loop import LoopConfig, run_agent_loop
from witty_agent.prompts import get_prompt
from witty_agent.tool_validation import ToolArgumentError, validate_tool_arguments
from witty_agent.tools.registry import ToolSpec, tool
from witty_agent.types import AgentContext, AgentMessage, ModelRef


def _spec(parameters: dict, name: str = "echo") -> ToolSpec:
    def echo(**kwargs: object) -> str:
        return str(kwargs)

    return ToolSpec(name=name, description="echo", parameters=parameters, func=echo)


class ValidateToolArgumentsTests(unittest.TestCase):
    def test_coerces_plain_json_primitives(self) -> None:
        cases = [
            ({"type": "number"}, "42", 42),
            ({"type": "number"}, True, 1),
            ({"type": "number"}, None, 0),
            ({"type": "integer"}, "42", 42),
            ({"type": "integer"}, "42.0", 42),
            ({"type": "boolean"}, "true", True),
            ({"type": "boolean"}, "false", False),
            ({"type": "boolean"}, 1, True),
            ({"type": "boolean"}, 0, False),
            ({"type": "string"}, None, ""),
            ({"type": "string"}, True, "true"),
            ({"type": "null"}, "", None),
            ({"type": "null"}, 0, None),
            ({"type": ["number", "string"]}, "1", "1"),
            ({"type": ["boolean", "number"]}, "1", 1),
        ]
        for schema, incoming, expected in cases:
            with self.subTest(schema=schema, incoming=incoming):
                tool_spec = _spec(
                    {
                        "type": "object",
                        "properties": {"value": schema},
                        "required": ["value"],
                    }
                )
                self.assertEqual(
                    validate_tool_arguments(tool_spec, {"value": incoming}),
                    {"value": expected},
                )

    def test_drops_optional_null_and_keeps_nullable(self) -> None:
        tool_spec = _spec(
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "metadata": {
                        "type": "object",
                        "properties": {"enabled": {"type": "boolean"}},
                    },
                },
                "required": ["path"],
            }
        )
        self.assertEqual(
            validate_tool_arguments(
                tool_spec,
                {
                    "path": "file.txt",
                    "offset": None,
                    "nullable": None,
                    "metadata": {"enabled": None},
                },
            ),
            {"path": "file.txt", "nullable": None, "metadata": {}},
        )

    def test_strips_extra_keys_when_additional_unspecified(self) -> None:
        tool_spec = _spec(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            }
        )
        self.assertEqual(
            validate_tool_arguments(tool_spec, {"path": "a.txt", "extra": 1}),
            {"path": "a.txt"},
        )

    def test_rejects_extra_keys_when_additional_false(self) -> None:
        tool_spec = _spec(
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            }
        )
        with self.assertRaises(ToolArgumentError) as ctx:
            validate_tool_arguments(tool_spec, {"path": "a.txt", "extra": 1})
        self.assertIn("unexpected property", str(ctx.exception))
        self.assertIn("extra", str(ctx.exception))

    def test_rejects_missing_required_and_bad_coercion(self) -> None:
        tool_spec = _spec(
            {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "flag": {"type": "boolean"},
                },
                "required": ["count"],
            }
        )
        with self.assertRaises(ToolArgumentError) as ctx:
            validate_tool_arguments(tool_spec, {"flag": "1"})
        text = str(ctx.exception)
        self.assertIn("count", text)
        self.assertIn("required property missing", text)

        with self.assertRaises(ToolArgumentError):
            validate_tool_arguments(tool_spec, {"count": "42.1"})
        with self.assertRaises(ToolArgumentError):
            validate_tool_arguments(tool_spec, {"count": 1, "flag": "1"})

    def test_generated_tool_schema_forbids_additional_properties(self) -> None:
        @tool
        def add_numbers(left: int, right: int = 1) -> int:
            """Add two integers.

            Args:
                left: First number
                right: Second number
            """
            return left + right

        spec = add_numbers._witty_tool
        self.assertIs(spec.parameters["additionalProperties"], False)
        self.assertEqual(validate_tool_arguments(spec, {"left": "2", "right": "3"}), {"left": 2, "right": 3})
        with self.assertRaises(ToolArgumentError):
            validate_tool_arguments(spec, {"left": 1, "bonus": 9})


class LoopToolValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_args_become_tool_error_not_typeerror(self) -> None:
        @tool
        def echo_path(path: str) -> str:
            """Echo a path.

            Args:
                path: File path
            """
            return path

        context = AgentContext(
            system_prompt="sys",
            messages=[],
            tools=[echo_path._witty_tool],
            workspace_dir=".",
            model=ModelRef(provider="openai", model_id="test"),
            project_id="grid-base",
            agent_id="coder",
            session_id="s1",
        )
        result = await run_agent_loop(
            [AgentMessage(role="user", content="echo")],
            context,
            ScriptedLLM(
                [
                    tool_reply("echo_path", {"bonus": 1}, call_id="c1"),
                    text_reply("retry-ok"),
                ]
            ),
            LoopConfig(approval_mode="allow-all", retry_attempts=1),
        )
        tools = [item for item in result.messages if item.role == "toolResult"]
        self.assertEqual(len(tools), 1)
        self.assertTrue(tools[0].is_error)
        # 报错正文是配置（prompts.toml invalid_tool_args），测试跟着配置走，不抄字面。
        head = get_prompt("invalid_tool_args", tool_name="echo_path", errors="-", received="{}").splitlines()[0]
        self.assertIn(head, tools[0].text())
        self.assertIn("path", tools[0].text())
        self.assertNotIn("TypeError", tools[0].text())


if __name__ == "__main__":
    unittest.main()
