"""Application-wide styles applied only when submitting still-image requests."""
from uuid import uuid4

from pydantic import BaseModel, Field
from PySide6.QtCore import QSettings


class VisualStyle(BaseModel):
    identifier: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    prompt: str


class VisualStylePreferences(BaseModel):
    styles: list[VisualStyle] = Field(default_factory=list)
    selected_id: str | None = None

    def selected_prompt(self) -> str:
        return next((style.prompt for style in self.styles
                     if style.identifier == self.selected_id), '')


def load_visual_styles() -> VisualStylePreferences:
    store = QSettings('ProseToVideo', 'ProseToVideo')
    return VisualStylePreferences.model_validate_json(str(store.value('visual_styles/preferences', '{}')))


def save_visual_styles(preferences: VisualStylePreferences) -> None:
    store = QSettings('ProseToVideo', 'ProseToVideo')
    store.setValue('visual_styles/preferences', preferences.model_dump_json())
    store.sync()
    if store.status() != type(store).Status.NoError:
        raise ValueError('Could not save visual styles')
