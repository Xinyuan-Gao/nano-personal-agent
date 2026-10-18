from __future__ import annotations

from ..core.config import AppConfig


def build_services(config: AppConfig | None = None):
    from ..api.services import AppServices

    return AppServices(config or AppConfig.load())
