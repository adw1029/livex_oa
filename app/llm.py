"""One `create()` call over either the Claude API or Amazon Bedrock — same Messages API, same code path."""

from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic, AsyncAnthropicBedrockMantle

from app.config import Settings


class LLM:
    def __init__(self, settings: Settings):
        self.provider = settings.llm_provider
        self.model = settings.model
        self.effort = settings.effort
        if self.provider == "bedrock":
            self.client: Any = AsyncAnthropicBedrockMantle(aws_region=settings.aws_region, aws_profile=settings.aws_profile)
        elif self.provider == "anthropic":
            self.client = AsyncAnthropic()
        else:
            raise ValueError(f"LLM_PROVIDER must be 'anthropic' or 'bedrock', got {self.provider!r}")

    async def create(self, *, system: Any, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any:
        return await self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
        )
