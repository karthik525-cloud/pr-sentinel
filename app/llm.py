"""Small provider boundary. The provider owns its native conversation history."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: object
    call_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    payload: dict[str, object]


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    calls: tuple[ToolCall, ...] = ()


class ReviewSession(Protocol):
    async def request(
        self,
        *,
        initial_context: str | None = None,
        tool_results: tuple[ToolResult, ...] = (),
        final: bool = False,
    ) -> ModelTurn: ...

    async def aclose(self) -> None: ...
