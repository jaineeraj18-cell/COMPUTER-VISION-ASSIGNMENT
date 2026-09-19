"""Tiny dataclass <-> JSON helpers.

We deliberately avoid a schema library here. The artifact is the contract other
systems depend on, so its shape is written out explicitly in `models.py` and in
`schema/capability.schema.json` rather than being an emergent property of a
library version.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / lists / dicts into JSON-safe values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: Dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            value = to_jsonable(getattr(obj, f.name))
            if value is None:
                continue  # keep artifacts readable: omit empty optionals
            out[f.name] = value
        return out
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    return obj


def opt(factory: Callable[[Dict[str, Any]], T], raw: Optional[Dict[str, Any]]) -> Optional[T]:
    return factory(raw) if raw else None


def many(factory: Callable[[Dict[str, Any]], T], raw: Optional[List[Any]]) -> List[T]:
    return [factory(item) for item in (raw or [])]


class SchemaError(ValueError):
    """Raised when an artifact does not satisfy the capability contract."""
