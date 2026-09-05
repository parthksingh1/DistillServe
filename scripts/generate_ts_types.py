"""Emit TypeScript declarations from the shared Pydantic models.

The Pydantic models in ``packages/schemas`` are the single source of truth for
every cross-process shape. This script projects them into TypeScript so the
frontend cannot drift from the backend, and so a breaking rename fails CI
rather than a user's browser.

The emitter is deliberately small and *strict*: it supports exactly the type
constructs the schemas use, and raises on anything else. A loose emitter that
silently degrades an unknown type to ``unknown`` would defeat the purpose.

Usage:
    uv run python scripts/generate_ts_types.py            # write the file
    uv run python scripts/generate_ts_types.py --check    # fail if stale (CI)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import types
import typing
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

import distillserve_schemas

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "apps" / "web" / "src" / "lib" / "generated" / "schemas.ts"

HEADER = """\
/**
 * GENERATED FILE — do not edit.
 *
 * Emitted from packages/schemas by `make schemas`. Edit the Pydantic models
 * and regenerate; CI runs `generate_ts_types.py --check` and fails on drift.
 */

"""

_PRIMITIVES: dict[type, str] = {
    str: "string",
    int: "number",
    float: "number",
    bool: "boolean",
    dt.datetime: "string",
    dt.date: "string",
    type(None): "null",
}


class UnsupportedTypeError(TypeError):
    """Raised when a model uses a construct the emitter does not handle."""


def _ts_type(annotation: object) -> str:
    """Render a Python annotation as a TypeScript type expression."""
    if annotation in _PRIMITIVES:
        return _PRIMITIVES[annotation]
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation.__name__
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        return " | ".join(dict.fromkeys(_ts_type(arg) for arg in args))
    if origin in (list, set, tuple, frozenset):
        if not args:
            raise UnsupportedTypeError(f"bare container annotation: {annotation!r}")
        return f"{_ts_type(args[0])}[]"
    if origin is dict:
        key, value = args
        return f"Record<{_ts_type(key)}, {_ts_type(value)}>"
    if origin is typing.Literal:
        return " | ".join(f'"{arg}"' for arg in args)

    raise UnsupportedTypeError(
        f"{annotation!r} is not supported by the emitter. Add a case in _ts_type()."
    )


def _doc_comment(text: str | None, indent: str = "") -> str:
    """Render a docstring or field description as a JSDoc block."""
    if not text:
        return ""
    lines = [line.strip() for line in text.strip().splitlines()]
    body = "\n".join(f"{indent} * {line}".rstrip() for line in lines)
    return f"{indent}/**\n{body}\n{indent} */\n"


def _emit_enum(enum_cls: type[Enum]) -> str:
    """Render a Python enum as a TS string-literal union.

    A union rather than a TS ``enum``: unions are erasable, structurally
    compatible with the JSON that actually arrives on the wire, and do not
    force the frontend to import a runtime value just to name a type.
    """
    members = " | ".join(f"'{member.value}'" for member in enum_cls)
    return f"{_doc_comment(enum_cls.__doc__)}export type {enum_cls.__name__} = {members};\n"


def _is_client_input(model: type[BaseModel]) -> bool:
    """Whether the frontend constructs this model rather than only reading it."""
    extra = model.model_config.get("json_schema_extra")
    return isinstance(extra, dict) and bool(extra.get("ts_client_input"))


def _field_line(name: str, field: FieldInfo, *, client_input: bool) -> str:
    """Render one interface property, including its description.

    Optionality depends on direction, and getting it wrong is a real ergonomic
    cost either way:

    * **Response models**: a Pydantic field with a default is still always
      *serialised*, so the key is always present on the wire. Emitting ``?``
      would force every call site to narrow a value that cannot be undefined.
    * **Client input models**: a defaulted field genuinely may be omitted by
      the caller, so ``?`` is correct there.

    Nullability is carried by the rendered type itself, as ``| null``.
    """
    rendered = _ts_type(field.annotation)
    optional = "?" if client_input and not field.is_required() else ""
    return f"{_doc_comment(field.description, '  ')}  {name}{optional}: {rendered};\n"


def _emit_model(model: type[BaseModel]) -> str:
    """Render a Pydantic model as a TS interface."""
    client_input = _is_client_input(model)
    body = "".join(
        _field_line(name, field, client_input=client_input)
        for name, field in model.model_fields.items()
    )
    return f"{_doc_comment(model.__doc__)}export interface {model.__name__} {{\n{body}}}\n"


def _literal(value: Any) -> str:
    """Render a Python scalar as a TypeScript literal."""
    if isinstance(value, Enum):
        return f"'{value.value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f"'{value}'"
    return str(value)


def _emit_constant(name: str, value: Mapping[Any, Any]) -> str:
    """Render a mapping constant as a typed TS const.

    Shared tables — the traffic percentage per rollout stage, say — belong on
    both sides of the wire. Emitting them stops the frontend re-declaring a
    table the backend owns and can change.
    """
    key_type = _ts_type(type(next(iter(value))))
    value_type = _ts_type(type(next(iter(value.values()))))
    entries = "".join(f"  {_literal(key)}: {_literal(item)},\n" for key, item in value.items())
    return f"export const {name}: Record<{key_type}, {value_type}> = {{\n{entries}}};\n"


def render() -> str:
    """Render the full generated module for everything exported by the package."""
    enums: list[str] = []
    models: list[str] = []
    constants: list[str] = []

    for name in distillserve_schemas.__all__:
        obj = getattr(distillserve_schemas, name)
        if isinstance(obj, type) and issubclass(obj, Enum):
            enums.append(_emit_enum(obj))
        elif isinstance(obj, type) and issubclass(obj, BaseModel):
            models.append(_emit_model(obj))
        elif isinstance(obj, Mapping):
            constants.append(_emit_constant(name, obj))
        else:
            raise UnsupportedTypeError(f"{name} is not an Enum, a BaseModel or a mapping constant.")

    return HEADER + "\n".join([*enums, *models, *constants])


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the checked-in file differs from the emitted output.",
    )
    args = parser.parse_args(argv)

    rendered = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(REPO_ROOT)} is stale. Run `make schemas`.", file=sys.stderr)
            return 1
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
