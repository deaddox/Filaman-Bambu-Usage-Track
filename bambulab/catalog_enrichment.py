from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CatalogEnrichmentMixin:
    """Lifecycle hooks for catalog enrichment in readonly-safe mode.

    Phase 1 keeps this intentionally lightweight: flags and health reporting
    are wired while behavior remains no-op unless explicitly enabled later.
    """

    def _register_catalog_enrichment(self) -> None:
        if not bool(getattr(self, "_resolve_shop_images", False)):
            return
        logger.info(
            "Catalog enrichment enabled for printer %s (readonly-safe mode)",
            getattr(self, "printer_id", "?"),
        )

    def _unregister_catalog_enrichment(self) -> None:
        if not bool(getattr(self, "_resolve_shop_images", False)):
            return
        logger.info(
            "Catalog enrichment disabled for printer %s",
            getattr(self, "printer_id", "?"),
        )

    def _catalog_health_payload(self) -> dict[str, Any]:
        return {
            "catalog_images_enabled": bool(getattr(self, "_resolve_shop_images", False)),
            "catalog_provider": "bambulab",
        }
