from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ResolvedProviderConfig:
    provider: str
    base_url: str
    model: str


@dataclass(frozen=True)
class ProviderModelInfo:
    id: str
    name: str


class CompletionProvider(Protocol):
    def validate_credentials(self, *, timeout: float | None = None) -> None: ...

    def complete(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 800,
        json_mode: bool = True,
        json_schema: dict[str, Any] | None = None,
    ) -> str | None: ...

    def close(self) -> None: ...
