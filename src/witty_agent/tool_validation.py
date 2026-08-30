"""工具参数校验：按 JSON Schema 校验并做常见类型强制转换。"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

from witty_agent.prompts import get_prompt


class ToolArgumentError(ValueError):
    """入参未通过工具 schema，循环应作为 is_error 工具结果回给模型。"""


def validate_tool_arguments(tool: object, arguments: object) -> dict[str, Any]:
    """校验并返回可 **kwargs 展开的参数。不改传入的 arguments。"""
    name = str(getattr(tool, "name", "") or "unknown")
    schema = getattr(tool, "parameters", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    if not isinstance(arguments, dict):
        raise ToolArgumentError(
            _format_error(
                name,
                ["  - root: expected object"],
                arguments,
            )
        )
    args = copy.deepcopy(arguments)
    _normalize_optional_nulls(args, schema)
    coerced = _coerce_with_schema(args, schema)
    if not isinstance(coerced, dict):
        coerced = args
    additional = schema.get("additionalProperties")
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if additional is not True:
        extras = [key for key in list(coerced) if key not in properties]
        if additional is False:
            pass
        else:
            for key in extras:
                coerced.pop(key, None)
    errors = _collect_errors(coerced, schema)
    if errors:
        raise ToolArgumentError(_format_error(name, errors, arguments))
    return coerced


def _format_error(name: str, errors: list[str], received: object) -> str:
    return get_prompt(
        "invalid_tool_args",
        tool_name=name,
        errors="\n".join(errors),
        received=json.dumps(received, ensure_ascii=False, indent=2),
    )


def _schema_types(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("type")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str)]
    return []


def _matches_json_type(value: object, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    return False


def _coerce_primitive(value: object, type_name: str) -> object:
    if type_name == "number":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            try:
                parsed = float(value)
            except ValueError:
                return value
            if math.isfinite(parsed):
                return int(parsed) if parsed == int(parsed) else parsed
        if isinstance(value, bool):
            return 1 if value else 0
        return value
    if type_name == "integer":
        if value is None:
            return 0
        if isinstance(value, str) and value.strip() != "":
            try:
                parsed = float(value)
            except ValueError:
                return value
            if math.isfinite(parsed) and parsed == int(parsed):
                return int(parsed)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, float) and value == int(value):
            return int(value)
        return value
    if type_name == "boolean":
        if value is None:
            return False
        if value == "true":
            return True
        if value == "false":
            return False
        if value == 1:
            return True
        if value == 0:
            return False
        return value
    if type_name == "string":
        if value is None:
            return ""
        if isinstance(value, (int, float, bool)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, bool):
            return "true" if value else "false"
        return value
    if type_name == "null":
        if value in ("", 0, False):
            return None
        return value
    return value


def _coerce_with_union(value: object, schemas: list[object]) -> object:
    candidates = [item for item in schemas if isinstance(item, dict)]
    for schema in candidates:
        if not _collect_errors(value, schema):
            return value
    for schema in candidates:
        coerced = _coerce_with_schema(copy.deepcopy(value), schema)
        if not _collect_errors(coerced, schema):
            return coerced
    return value


def _coerce_with_schema(value: object, schema: dict[str, Any]) -> object:
    next_value = value
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for nested in all_of:
            if isinstance(nested, dict):
                next_value = _coerce_with_schema(next_value, nested)
    for key in ("anyOf", "oneOf"):
        union = schema.get(key)
        if isinstance(union, list):
            next_value = _coerce_with_union(next_value, union)
    types = _schema_types(schema)
    matches_union = len(types) > 1 and any(_matches_json_type(next_value, item) for item in types)
    if types and not matches_union:
        for type_name in types:
            candidate = _coerce_primitive(next_value, type_name)
            if candidate is not next_value:
                next_value = candidate
                break
    if "object" in types and isinstance(next_value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for key, prop_schema in properties.items():
            if key in next_value and isinstance(prop_schema, dict):
                next_value[key] = _coerce_with_schema(next_value[key], prop_schema)
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            defined = set(properties)
            for key, item in list(next_value.items()):
                if key not in defined:
                    next_value[key] = _coerce_with_schema(item, additional)
    if "array" in types and isinstance(next_value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            next_value = [_coerce_with_schema(item, items) for item in next_value]
        elif isinstance(items, list):
            next_value = list(next_value)
            for index, item_schema in enumerate(items):
                if index < len(next_value) and isinstance(item_schema, dict):
                    next_value[index] = _coerce_with_schema(next_value[index], item_schema)
    return next_value


def _normalize_optional_nulls(value: object, schema: dict[str, Any]) -> None:
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for item in value:
                _normalize_optional_nulls(item, items)
        elif isinstance(items, list):
            for index, item_schema in enumerate(items):
                if index < len(value) and isinstance(item_schema, dict):
                    _normalize_optional_nulls(value[index], item_schema)
        return
    if not isinstance(value, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    required = set(schema.get("required") or [])
    for key, prop_schema in properties.items():
        if key not in value or not isinstance(prop_schema, dict):
            continue
        if value[key] is None and key not in required and "$ref" not in prop_schema:
            if _nullable(prop_schema):
                _normalize_optional_nulls(value[key], prop_schema)
            else:
                del value[key]
        else:
            _normalize_optional_nulls(value[key], prop_schema)


def _nullable(schema: dict[str, Any]) -> bool:
    if "null" in _schema_types(schema):
        return True
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            for option in options:
                if isinstance(option, dict) and _schema_types(option) == ["null"]:
                    return True
                if isinstance(option, dict) and "null" in _schema_types(option):
                    return True
    return False


def _path(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key


def _collect_errors(value: object, schema: dict[str, Any], path: str = "") -> list[str]:
    if "$ref" in schema:
        return []
    errors: list[str] = []
    types = _schema_types(schema)
    unions: list[dict[str, Any]] = []
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            unions.extend(item for item in options if isinstance(item, dict))
    if unions:
        if any(not _collect_errors(value, option) for option in unions):
            return []
        label = path or "root"
        errors.append(f"  - {label}: did not match any allowed schema")
        return errors
    if types and not any(_matches_json_type(value, item) for item in types):
        label = path or "root"
        errors.append(f"  - {label}: expected {' or '.join(types)}")
        return errors
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        label = path or "root"
        errors.append(f"  - {label}: expected one of {enum}")
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        for key in required:
            if key not in value:
                errors.append(f"  - {_path(path, str(key))}: required property missing")
        for key, prop_schema in properties.items():
            if key in value and isinstance(prop_schema, dict):
                errors.extend(_collect_errors(value[key], prop_schema, _path(path, str(key))))
        additional = schema.get("additionalProperties")
        if additional is False:
            for key in value:
                if key not in properties:
                    errors.append(f"  - {_path(path, str(key))}: unexpected property")
        elif isinstance(additional, dict):
            for key, item in value.items():
                if key not in properties:
                    errors.extend(_collect_errors(item, additional, _path(path, str(key))))
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                child = f"{path}[{index}]" if path else f"[{index}]"
                errors.extend(_collect_errors(item, items, child))
        elif isinstance(items, list):
            for index, item_schema in enumerate(items):
                if index < len(value) and isinstance(item_schema, dict):
                    child = f"{path}[{index}]" if path else f"[{index}]"
                    errors.extend(_collect_errors(value[index], item_schema, child))
    return errors
