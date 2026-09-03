from __future__ import annotations

from typing import Any


class CatalogMixin:
    """Catalog-image feature flags and lightweight metadata helpers.

    This mixin keeps the driver readonly-safe: it only prepares metadata values
    and does not trigger printer-side write operations.
    """

    def _normalize_article_number(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if len(text) == 5 and text.isdigit():
            return text
        return None

    def _catalog_feature_state(self) -> dict[str, Any]:
        return {
            "resolve_shop_images": bool(getattr(self, "_resolve_shop_images", False)),
            "provider": "bambulab",
        }
