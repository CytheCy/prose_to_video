"""Interchangeable language-model provider interface and OpenAI adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class BoundaryProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts: list[int]


class SceneDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    location: str | None
    time_of_day: str | None
    characters: list[str]
    point_of_view: str | None
    narrative_type: str | None
    summary: str
    environment: str
    important_objects: list[str]
    actions: list[str]
    dialogue_participants: list[str]
    visual_atmosphere: str
    boundary_reason: str


@dataclass(frozen=True)
class AISettings:
    provider: str = "OpenAI"
    model: str = "gpt-6-luna"
    credential_env: str = "OPENAI_API_KEY"
    temperature: float | None = None
    max_output_tokens: int = 2500
    context_tokens: int = 128000
    chunk_chars: int = 12000
    overlap_chars: int = 1500

    def validate(self) -> None:
        if self.provider != "OpenAI":
            raise ValueError(f"Unsupported AI provider: {self.provider}")
        if not self.model.strip() or not self.credential_env.isidentifier():
            raise ValueError("Enter a model and a valid credential environment variable name")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("Temperature must be between 0 and 2")
        if (self.max_output_tokens < 256 or self.context_tokens < self.max_output_tokens + 3000
                or self.chunk_chars < 1000 or self.overlap_chars < 0
                or self.overlap_chars * 2 >= self.chunk_chars):
            raise ValueError("Invalid AI context, output, or chunk settings")


class SceneProvider(Protocol):
    def propose_boundaries(self, labeled_text: str) -> BoundaryProposal: ...
    def describe_scene(self, scene_text: str) -> SceneDescription: ...


class OpenAIProvider:
    def __init__(self, settings: AISettings):
        settings.validate()
        key = os.environ.get(settings.credential_env)
        if not key:
            raise ValueError(f"Set the {settings.credential_env} environment variable before detecting scenes")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ValueError("OpenAI client is unavailable. Install the project's dependencies.") from exc
        self.client = OpenAI(api_key=key, timeout=60.0, max_retries=2)
        self.settings = settings

    def _request(self, instructions: str, content: str, schema: type[BaseModel]) -> BaseModel:
        # UTF-8 bytes are a conservative upper bound for text tokens in the
        # provider's byte-based tokenizer. Reserve room for schema and framing.
        estimated_input = len((instructions + content).encode("utf-8")) + 2000
        if estimated_input + self.settings.max_output_tokens > self.settings.context_tokens:
            raise ValueError("AI input exceeds the configured model context. Reduce chunk size or output limit.")
        kwargs = {
            "model": self.settings.model,
            "instructions": instructions,
            "input": content,
            "text_format": schema,
            "max_output_tokens": self.settings.max_output_tokens,
            "store": False,
            "truncation": "disabled",
        }
        if self.settings.temperature is not None:
            kwargs["temperature"] = self.settings.temperature
        try:
            response = self.client.responses.parse(**kwargs)
        except Exception as exc:
            # Avoid storing provider exception payloads, which can contain prose or secrets.
            kind = type(exc).__name__
            explanations = {
                "AuthenticationError": "Check the API credential.",
                "RateLimitError": "The provider rate limit or quota was reached.",
                "APITimeoutError": "The AI request timed out. Try again or use smaller chunks.",
                "APIConnectionError": "Could not connect to the AI provider.",
            }
            raise ValueError(f"AI request failed ({kind}). {explanations.get(kind, 'Check the model, network, and settings.')}") from exc
        if not isinstance(response.output_parsed, schema):
            raise ValueError("AI returned no usable structured response")
        return response.output_parsed

    def propose_boundaries(self, labeled_text: str) -> BoundaryProposal:
        return self._request(
            "Find meaningful narrative scene starts. Respond with paragraph/segment IDs only. "
            "Use changes in location, point of view, time, character group, event sequence, "
            "distinct narrative section, or explicit scene breaks. Keep continuous action and "
            "conversation together. Do not split by length. Only choose IDs marked eligible. "
            "Do not include the first ID unless it is a true scene transition from prior context.",
            labeled_text, BoundaryProposal,
        )

    def describe_scene(self, scene_text: str) -> SceneDescription:
        return self._request(
            "Extract factual scene information from the provided prose. Narrative type may be "
            "action, dialogue, exposition, news report, flashback, or another supported category. "
            "Explain the boundary only if the prose supports it. Use a concise summary, "
            "detailed grounded environment and actions. Do not invent appearance, setting, "
            "time of day, or events. Use null for unknown optional fields and empty lists/strings "
            "for unknown descriptive fields. Give a short useful title.",
            scene_text, SceneDescription,
        )

    def propose_shots(self, payload: str):
        from prose_to_video.pipeline.shot_planning import ShotIdeas
        return self._request(
            "Plan cinematic visual shots from labeled source sentences. Select ID breakpoints based on "
            "visible changes in action, focus, dialogue, setting, or emotion; do not divide mechanically. "
            "The first chunk must include ID 0. Subsequent chunks need no first-ID shot unless a visual "
            "change really occurs there. Preserve narrative chronology. Depict only events supported by "
            "the assigned passage; never reveal later events. Ground characters, clothing and props in "
            "the source and keep recurring identities consistent. Make foreground, background, actions "
            "and visual description concrete. Cinematic interpretation may fill framing and lighting, "
            "but do not invent major facts. Use only valid IDs and controlled shot types and camera "
            "movements. Do not return or invent timestamps.",
            payload, ShotIdeas)


def make_provider(settings: AISettings) -> SceneProvider:
    settings.validate()
    return OpenAIProvider(settings)
