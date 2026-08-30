"""工具注册：用 @tool 标函数，按包扫描。描述和 JSON Schema 来自 docstring / 类型注解。"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from witty_agent.kernel_surface import is_kernel_tool, is_kernel_tool_module
from witty_agent.logging import get_logger
from witty_agent.runtime import tool_packages

logger = get_logger("tools")

_ATTR = "_witty_tool"
_PRIMITIVES = {str: "string", int: "integer", float: "number", bool: "boolean"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    timeout_ms: int | None = None


_REGISTERED: dict[str, ToolSpec] = {}


def register_tool(spec: ToolSpec) -> ToolSpec:
    """登记一份完整 schema 的工具。内核工具只允许 witty_agent.tools 安装或刷新同实现。"""
    caller = ""
    frame = inspect.currentframe()
    if frame is not None and frame.f_back is not None:
        caller = str(frame.f_back.f_globals.get("__name__") or "")
    existing = _REGISTERED.get(spec.name)
    if is_kernel_tool(spec.name):
        if caller and not is_kernel_tool_module(caller):
            logger.warning("拒绝登记内核工具 name=%s caller=%s", spec.name, caller)
            return existing or spec
        if existing is not None and existing.func is not spec.func:
            logger.warning("拒绝覆盖内核工具 name=%s", spec.name)
            return existing
    _REGISTERED[spec.name] = spec
    return spec


def _unwrap_optional(annotation: object) -> tuple[object, bool]:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [item for item in get_args(annotation) if item is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _json_type(annotation: object) -> dict[str, Any]:
    inner, _optional = _unwrap_optional(annotation)
    origin = get_origin(inner)
    if origin is list:
        args = get_args(inner)
        item_schema = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item_schema}
    if origin is dict:
        return {"type": "object"}
    if inner in _PRIMITIVES:
        return {"type": _PRIMITIVES[inner]}
    return {"type": "string"}


def _parse_arg_docs(doc: str) -> dict[str, str]:
    docs: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        if in_args and stripped.lower().endswith(":") and not stripped[0:1].isalnum():
            break
        if in_args and ":" in stripped:
            name, text = stripped.split(":", 1)
            docs[name.strip()] = text.strip()
    return docs


def _description_from_doc(doc: str | None) -> str:
    if not doc:
        return ""
    parts: list[str] = []
    for line in doc.strip().splitlines():
        if line.strip().lower().startswith(("args:", "returns:", "raises:")):
            break
        parts.append(line.strip())
    return " ".join(part for part in parts if part)


def _schema_for(func: Callable[..., Any], description: str) -> dict[str, Any]:
    hints = get_type_hints(func)
    signature = inspect.signature(func)
    arg_docs = _parse_arg_docs(inspect.getdoc(func) or "")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        if name in {"self", "cls"}:
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        annotation = hints.get(name, str)
        inner, optional = _unwrap_optional(annotation)
        schema = _json_type(inner)
        if name in arg_docs:
            schema["description"] = arg_docs[name]
        properties[name] = schema
        if param.default is inspect.Parameter.empty and not optional:
            required.append(name)
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "name": func.__name__,
        "description": description,
        "parameters": parameters,
    }


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    timeout_ms: int | None = None,
):
    """标记可被模型调用的工具。用法与常见 @tool 一致，不依赖 LangChain。"""

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        description = _description_from_doc(inspect.getdoc(target))
        if not description:
            raise ValueError(f"工具 {target.__name__} 必须有说明 docstring")
        spec = _schema_for(target, description)
        tool_spec = ToolSpec(
            name=name or spec["name"],
            description=description,
            parameters=spec["parameters"],
            func=target,
            timeout_ms=timeout_ms,
        )
        setattr(target, _ATTR, tool_spec)
        return target

    if func is None:
        return decorate
    return decorate(func)


def _iter_modules(package_name: str) -> list[ModuleType]:
    package = importlib.import_module(package_name)
    modules = [package]
    package_path = getattr(package, "__path__", None)
    if not package_path:
        return modules
    for module_info in pkgutil.walk_packages(package_path, prefix=f"{package_name}."):
        # `__main__` 是命令行入口，不会有 @tool，而且按惯例在模块层就跑 argparse。
        # 扫描时把它导进来等于替用户执行了一次 CLI。
        if module_info.name.rsplit(".", 1)[-1] == "__main__":
            continue
        try:
            modules.append(importlib.import_module(module_info.name))
        except (Exception, SystemExit) as exc:
            # 必须连 SystemExit 一起接：它是 BaseException，只接 Exception 的话，任何一个
            # 在导入期 `sys.exit()` 的业务模块都会把整条调用链掀掉——扫描发生在会话运行里，
            # 结果是 worker 线程静默死亡、run 永远停在 running，之后每次发送都 409。
            logger.warning("工具模块加载失败 module=%s err=%r", module_info.name, exc)
    return modules


def list_tools() -> list[ToolSpec]:
    """扫描 runtime 里声明的工具包。内核工具名不可被业务包覆盖。"""
    from witty_agent.plugins.live import disabled_packages

    blocked = disabled_packages()
    by_name: dict[str, ToolSpec] = {}
    for package_name in tool_packages():
        try:
            modules = _iter_modules(package_name)
        except ModuleNotFoundError as exc:
            logger.warning("工具包不存在 package=%s err=%s", package_name, exc)
            continue
        for module in modules:
            mod_name = module.__name__
            if any(mod_name == pkg or mod_name.startswith(f"{pkg}.") for pkg in blocked):
                continue
            for _attr, value in inspect.getmembers(module):
                spec = getattr(value, _ATTR, None)
                if not isinstance(spec, ToolSpec):
                    continue
                if is_kernel_tool(spec.name) and not is_kernel_tool_module(module.__name__):
                    logger.warning(
                        "拒绝覆盖内核工具 name=%s module=%s", spec.name, module.__name__
                    )
                    continue
                if is_kernel_tool(spec.name) and spec.name in by_name:
                    if by_name[spec.name].func is not spec.func:
                        logger.warning("拒绝覆盖内核工具 name=%s", spec.name)
                        continue
                by_name[spec.name] = spec
                logger.info(
                    "发现工具 name=%s module=%s", spec.name, module.__name__
                )
    for name, spec in _REGISTERED.items():
        if is_kernel_tool(name) and name in by_name and by_name[name].func is not spec.func:
            logger.warning("拒绝覆盖内核工具 name=%s source=register_tool", name)
            continue
        module = str(getattr(spec.func, "__module__", "") or "")
        if not is_kernel_tool(name) and any(
            module == pkg or module.startswith(f"{pkg}.") for pkg in blocked
        ):
            continue
        by_name[name] = spec
    if "todo_write" in by_name:
        from witty_agent.tools.todo import refresh_todo_tool

        by_name["todo_write"] = refresh_todo_tool()
    tools = list(by_name.values())
    logger.info("工具加载完成 count=%s", len(tools))
    return tools


def unregister_tool(name: str) -> None:
    """卸下一份 register_tool 登记。内核名拒绝。"""
    if is_kernel_tool(name):
        raise ValueError(f"内核工具不可卸载: {name}")
    _REGISTERED.pop(name, None)
    logger.info("卸下工具 name=%s", name)


def get_tool(name: str) -> ToolSpec:
    tools = {item.name: item for item in list_tools()}
    try:
        return tools[name]
    except KeyError as exc:
        known = ", ".join(sorted(tools)) or "(空)"
        raise KeyError(f"未找到工具 {name!r}，已有: {known}") from exc
