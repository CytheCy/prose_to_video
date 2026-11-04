"""Application defaults for text-to-image provider selection."""

from PySide6.QtCore import QSettings

from prose_to_video.models.image_job import ImageGenerationSettings


def load_tti_defaults() -> dict[str, str]:
    store = QSettings("ProseToVideo", "ProseToVideo")
    defaults = ImageGenerationSettings()
    return {key: str(store.value(f"tti/{key}", getattr(defaults, key)))
            for key in ("provider", "model")}


def save_tti_defaults(provider: str, model: str) -> None:
    store = QSettings("ProseToVideo", "ProseToVideo")
    store.setValue("tti/provider", provider)
    store.setValue("tti/model", model)
    store.sync()
    if store.status() != type(store).Status.NoError:
        raise ValueError("Could not save text-to-image settings")
