from __future__ import annotations

import json
import os
import re
from typing import Any


class CatalogMixin:
    """Catalog helpers for readonly-safe metadata enrichment."""

    _catalog_material_name_by_code: dict[str, str] | None = None

    @classmethod
    def _load_material_name_index(cls) -> dict[str, str]:
        if cls._catalog_material_name_by_code is not None:
            return cls._catalog_material_name_by_code

        catalog_path = os.path.join(os.path.dirname(__file__), "bambu_filaments.json")
        try:
            with open(catalog_path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                cls._catalog_material_name_by_code = {
                    str(key).strip().upper(): str(value).strip()
                    for key, value in raw.items()
                    if str(key).strip() and str(value).strip()
                }
            else:
                cls._catalog_material_name_by_code = {}
        except Exception:
            cls._catalog_material_name_by_code = {}

        return cls._catalog_material_name_by_code

    @staticmethod
    def _normalize_article_number(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if len(text) == 5 and text.isdigit():
            return text
        return None

    @staticmethod
    def _extract_material_code(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().upper()
        if not text:
            return None

        # Match Bambu material identifiers such as GFA00 or GFSNL05.
        match = re.search(r"\bGF[A-Z0-9]{3,8}\b", text)
        if match:
            return match.group(0)
        return None

    def _resolve_catalog_image_url(self, slot: dict[str, Any], material_code: str) -> str | None:
        existing = str(slot.get("catalog_image_url") or "").strip()
        if existing:
            return existing

        if not bool(getattr(self, "_resolve_shop_images", False)):
            return None

        template = str(getattr(self, "_shop_image_url_template", "") or "").strip()
        if not template:
            return None

        material_name = str(slot.get("catalog_material_name") or "").strip()
        if not material_name:
            material_name = self._load_material_name_index().get(material_code, "")

        return (
            template.replace("{code}", material_code)
            .replace("{name}", material_name)
            .replace(" ", "%20")
        )

    def _enrich_slot_catalog_metadata(self, slot: dict[str, Any]) -> None:
        tray_info_idx = slot.get("tray_info_idx")
        material_code = self._extract_material_code(tray_info_idx)
        material_name = None
        if material_code:
            material_name = self._load_material_name_index().get(material_code)

        slot["catalog_provider"] = "bambulab"
        if material_code:
            slot["catalog_material_code"] = material_code
        else:
            slot.pop("catalog_material_code", None)

        if material_name:
            slot["catalog_material_name"] = material_name
        else:
            slot.pop("catalog_material_name", None)

        if material_code:
            image_url = self._resolve_catalog_image_url(slot, material_code)
        else:
            image_url = None

        if image_url:
            slot["catalog_image_url"] = image_url
        else:
            slot.pop("catalog_image_url", None)

    def _enrich_slots_catalog_metadata(self, slots: list[dict[str, Any]]) -> None:
        for slot in slots:
            self._enrich_slot_catalog_metadata(slot)

    def _catalog_feature_state(self) -> dict[str, Any]:
        return {
            "resolve_shop_images": bool(getattr(self, "_resolve_shop_images", False)),
            "provider": "bambulab",
            "known_materials": len(self._load_material_name_index()),
            "image_template_configured": bool(
                str(getattr(self, "_shop_image_url_template", "") or "").strip()
            ),
        }
