"""A tiny name -> capability registry.

Region shapes, surfaces and planners all follow the same pattern: a class with
an "id", a human readable "label", an optional description and a ParameterSet.
The Registry collects them, builds the /api/catalog payload and resolves build
requests.  A contributor registering a new class therefore gets the HTTP API,
the UI panel and the documentation entry for free.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from toolpath_lab.core.errors import RegistryError

T = TypeVar("T")


class Registry(Generic[T]):
    """Ordered registry of capability classes keyed by their id."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._items: dict[str, T] = {}

    def register(self, cls: T) -> T:
        """Class decorator registering cls under its id."""

        item_id = getattr(cls, "id", "")
        if not isinstance(item_id, str) or not item_id:
            raise RegistryError(f"{cls!r} does not define a non-empty 'id'")
        if item_id in self._items:
            raise RegistryError(f"duplicate {self.kind} id {item_id!r}")
        self._items[item_id] = cls
        return cls

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def ids(self) -> list[str]:
        return list(self._items)

    def get(self, item_id: str) -> T:
        try:
            return self._items[item_id]
        except KeyError as error:
            known = ", ".join(self._items) or "<none>"
            raise RegistryError(
                f"unknown {self.kind} {item_id!r}; registered: {known}"
            ) from error

    def catalog(self) -> list[dict[str, Any]]:
        """JSON description of every registered capability."""

        entries: list[dict[str, Any]] = []
        for item_id, cls in self._items.items():
            parameters = getattr(cls, "parameters", None)
            entries.append(
                {
                    "id": item_id,
                    "label": getattr(cls, "label", item_id),
                    "description": getattr(cls, "description", ""),
                    "parameters": parameters.to_dicts() if parameters else [],
                }
            )
        return entries

