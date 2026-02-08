"""Risk taxonomy and measures catalog manager.

Loads formalized risk classifications and safety measures from YAML
configuration and provides lookup, validation, and scoring methods.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "taxonomy"


class TaxonomyManager:
    """Manages the risk taxonomy and measures catalog.

    Loads ``taxonomy.yaml`` and ``measures.yaml`` from the config directory
    and provides lookup, validation, severity calculation, and prompt
    context generation.
    """

    def __init__(
        self,
        taxonomy_path: Path | str | None = None,
        measures_path: Path | str | None = None,
    ) -> None:
        t_path = Path(taxonomy_path) if taxonomy_path else _CONFIG_DIR / "taxonomy.yaml"
        m_path = Path(measures_path) if measures_path else _CONFIG_DIR / "measures.yaml"

        self._taxonomy: dict[str, Any] = yaml.safe_load(
            t_path.read_text(encoding="utf-8")
        )
        self._measures_raw: dict[str, Any] = yaml.safe_load(
            m_path.read_text(encoding="utf-8")
        )

        # Build fast lookup indexes
        self._class_index: dict[str, dict[str, Any]] = {}  # class_name -> {rule_id, category, ...}
        self._rule_index: dict[str, str] = {}               # rule_id -> class_name
        self._measures: dict[str, dict[str, Any]] = self._measures_raw.get("measures", {})
        self._thresholds: dict[str, int] = (
            self._taxonomy.get("severity_matrix", {}).get("thresholds", {})
        )

        for cat_name, cat_data in self._taxonomy.get("categories", {}).items():
            for cls_name, cls_data in cat_data.get("classes", {}).items():
                self._class_index[cls_name] = {
                    "category": cat_name,
                    "rule_id": cls_data["rule_id"],
                    "description": cls_data.get("description", ""),
                }
                self._rule_index[cls_data["rule_id"]] = cls_name

        logger.info(
            "TaxonomyManager loaded: %d classes, %d measures",
            len(self._class_index),
            len(self._measures),
        )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get_class(self, class_name: str) -> dict[str, Any] | None:
        """Return class info or None if not found."""
        return self._class_index.get(class_name.upper())

    def get_rule_id(self, class_name: str) -> str:
        """Return the rule_id for a class, or empty string."""
        info = self._class_index.get(class_name.upper())
        return info["rule_id"] if info else ""

    def get_category_for_class(self, class_name: str) -> str:
        """Return the parent category for a class, or 'UNKNOWN'."""
        info = self._class_index.get(class_name.upper())
        return info["category"] if info else "UNKNOWN"

    def is_valid_class(self, class_name: str) -> bool:
        """Check if a class name exists in the taxonomy."""
        return class_name.upper() in self._class_index

    def all_class_names(self) -> list[str]:
        """Return all known class names."""
        return list(self._class_index.keys())

    # ------------------------------------------------------------------
    # Measures
    # ------------------------------------------------------------------

    def get_measure(self, code: str) -> dict[str, Any] | None:
        """Return full measure info by code, or None."""
        m = self._measures.get(code.upper()) or self._measures.get(code)
        if m is None:
            return None
        return {"code": code.upper(), **m}

    def get_measures_for_class(self, class_name: str) -> list[dict[str, Any]]:
        """Return all measures applicable to a given risk class."""
        cls_upper = class_name.upper()
        result = []
        for code, m in self._measures.items():
            if cls_upper in [a.upper() for a in m.get("applies_to", [])]:
                result.append({"code": code, **m})
        return result

    def resolve_measure_codes(self, codes: list[str]) -> list[dict[str, Any]]:
        """Resolve a list of measure codes into full measure objects.

        Unknown codes are silently skipped.
        """
        resolved = []
        for code in codes:
            m = self.get_measure(code)
            if m:
                resolved.append(m)
        return resolved

    # ------------------------------------------------------------------
    # Severity calculation
    # ------------------------------------------------------------------

    def calculate_severity(self, likelihood: int, impact: int) -> str:
        """Calculate severity level from likelihood x impact.

        Returns one of: 'critical', 'high', 'medium', 'low', 'info'.
        """
        likelihood = max(1, min(5, likelihood))
        impact = max(1, min(5, impact))
        score = likelihood * impact

        # Thresholds are ordered from highest to lowest
        for level in ("critical", "high", "medium", "low", "info"):
            threshold = self._thresholds.get(level, 0)
            if score >= threshold:
                return level
        return "info"

    # ------------------------------------------------------------------
    # Prompt context generation
    # ------------------------------------------------------------------

    def summary_for_prompt(self) -> str:
        """Generate a compact taxonomy summary suitable for LLM prompts.

        Includes all categories, classes with rule_ids, and measure codes.
        """
        lines = ["RISK TAXONOMY:", ""]

        for cat_name, cat_data in self._taxonomy.get("categories", {}).items():
            lines.append(f"Category: {cat_name}")
            for cls_name, cls_data in cat_data.get("classes", {}).items():
                lines.append(
                    f"  - {cls_name} ({cls_data['rule_id']}): {cls_data.get('description', '')}"
                )
            lines.append("")

        lines.append("SEVERITY SCORING:")
        lines.append("  likelihood (1-5) x impact (1-5) = score")
        lines.append("  critical: score >= 16, high: >= 10, medium: >= 5, low: >= 2, info: < 2")
        lines.append("")

        lines.append("AVAILABLE MEASURES:")
        for code, m in self._measures.items():
            applies = ", ".join(m.get("applies_to", []))
            lines.append(f"  - {code}: {m['title']} (for: {applies})")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def validate_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalize a raw LLM finding against the taxonomy.

        - Validates risk_class against known classes (fallback: keep as-is)
        - Looks up rule_id from class if missing
        - Resolves measure_codes to full measure objects
        - Calculates severity from likelihood x impact
        """
        risk_class = (finding.get("risk_class") or "").upper()
        category = (finding.get("category") or "").upper()

        # Validate class and auto-fill rule_id/category
        if self.is_valid_class(risk_class):
            cls_info = self._class_index[risk_class]
            if not finding.get("rule_id"):
                finding["rule_id"] = cls_info["rule_id"]
            if not category or category == "UNKNOWN":
                finding["category"] = cls_info["category"]
        else:
            if not finding.get("rule_id"):
                finding["rule_id"] = ""

        finding["risk_class"] = risk_class

        # Calculate severity from likelihood x impact
        likelihood = int(finding.get("likelihood") or 1)
        impact = int(finding.get("impact") or 1)
        finding["likelihood"] = max(1, min(5, likelihood))
        finding["impact"] = max(1, min(5, impact))
        finding["risk_level"] = self.calculate_severity(
            finding["likelihood"], finding["impact"]
        )

        # Resolve measures
        raw_codes = finding.pop("measure_codes", []) or []
        resolved = self.resolve_measure_codes(raw_codes)
        # Also add auto-suggested measures based on class
        if risk_class and self.is_valid_class(risk_class) and not resolved:
            resolved = self.get_measures_for_class(risk_class)
        finding["measures"] = resolved

        return finding


@lru_cache
def get_taxonomy_manager() -> TaxonomyManager:
    """Return a cached singleton ``TaxonomyManager``."""
    return TaxonomyManager()
