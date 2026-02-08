"""Tests for risk taxonomy, measures catalog, scoring engine, and TaxonomyManager."""

import pytest

from services.taxonomy import TaxonomyManager, get_taxonomy_manager


# ===================================================================
# TaxonomyManager Loading
# ===================================================================


class TestTaxonomyLoading:
    """Tests for YAML loading and basic lookups."""

    def test_loads_successfully(self):
        tm = TaxonomyManager()
        assert len(tm.all_class_names()) > 0

    def test_all_physical_classes_present(self):
        tm = TaxonomyManager()
        physical = [
            "STUNTS", "FALLS", "FIGHTS", "WEAPONS", "VEHICLES",
            "HEIGHT", "WATER", "FIRE", "ELECTRICAL", "ANIMALS",
            "WEATHER", "FATIGUE", "CROWD",
        ]
        for cls in physical:
            assert tm.is_valid_class(cls), f"Missing physical class: {cls}"

    def test_all_environmental_classes_present(self):
        tm = TaxonomyManager()
        environmental = ["DANGEROUS_LOCATION", "CONFINED_SPACE", "SMOKE_DUST", "NOISE"]
        for cls in environmental:
            assert tm.is_valid_class(cls), f"Missing environmental class: {cls}"

    def test_all_psychological_classes_present(self):
        tm = TaxonomyManager()
        psychological = [
            "VIOLENCE", "DEATH_GRIEF", "TRAUMA", "SEXUALIZED",
            "DISCRIMINATION", "INTIMACY",
        ]
        for cls in psychological:
            assert tm.is_valid_class(cls), f"Missing psychological class: {cls}"

    def test_total_class_count(self):
        tm = TaxonomyManager()
        # 13 physical + 4 environmental + 6 psychological = 23
        assert len(tm.all_class_names()) == 23

    def test_invalid_class(self):
        tm = TaxonomyManager()
        assert not tm.is_valid_class("NONEXISTENT")
        assert tm.get_class("NONEXISTENT") is None


# ===================================================================
# Class Lookups
# ===================================================================


class TestClassLookups:
    """Tests for class info, rule IDs, and category lookups."""

    def test_get_class_info(self):
        tm = TaxonomyManager()
        info = tm.get_class("FIRE")
        assert info is not None
        assert info["rule_id"] == "SEC-P-008"
        assert info["category"] == "PHYSICAL"

    def test_get_rule_id(self):
        tm = TaxonomyManager()
        assert tm.get_rule_id("FIRE") == "SEC-P-008"
        assert tm.get_rule_id("INTIMACY") == "SEC-Y-006"
        assert tm.get_rule_id("NOISE") == "SEC-E-004"
        assert tm.get_rule_id("NONEXISTENT") == ""

    def test_get_category_for_class(self):
        tm = TaxonomyManager()
        assert tm.get_category_for_class("STUNTS") == "PHYSICAL"
        assert tm.get_category_for_class("CONFINED_SPACE") == "ENVIRONMENTAL"
        assert tm.get_category_for_class("VIOLENCE") == "PSYCHOLOGICAL"
        assert tm.get_category_for_class("NONEXISTENT") == "UNKNOWN"

    def test_case_insensitive(self):
        tm = TaxonomyManager()
        assert tm.is_valid_class("fire")
        assert tm.is_valid_class("Fire")
        assert tm.get_rule_id("fire") == "SEC-P-008"


# ===================================================================
# Measures Catalog
# ===================================================================


class TestMeasuresCatalog:
    """Tests for measures lookup and resolution."""

    def test_get_measure(self):
        tm = TaxonomyManager()
        m = tm.get_measure("RIG-SAFETY")
        assert m is not None
        assert m["title"] == "Rigging und Sicherungsseile"
        assert m["responsible"] == "Stunt Coordination"
        assert "HEIGHT" in m["applies_to"]

    def test_get_measure_unknown(self):
        tm = TaxonomyManager()
        assert tm.get_measure("NONEXISTENT") is None

    def test_get_measures_for_class_fire(self):
        tm = TaxonomyManager()
        measures = tm.get_measures_for_class("FIRE")
        codes = [m["code"] for m in measures]
        assert "SFX-CLEARANCE" in codes
        assert "FIRE-DEPT" in codes
        assert "MEDICAL-STANDBY" in codes

    def test_get_measures_for_class_intimacy(self):
        tm = TaxonomyManager()
        measures = tm.get_measures_for_class("INTIMACY")
        codes = [m["code"] for m in measures]
        assert "INTIMACY-COORD" in codes
        assert "CLOSED-SET" in codes

    def test_resolve_measure_codes(self):
        tm = TaxonomyManager()
        resolved = tm.resolve_measure_codes(["RIG-SAFETY", "MEDICAL-STANDBY", "NONEXISTENT"])
        assert len(resolved) == 2
        assert resolved[0]["code"] == "RIG-SAFETY"
        assert resolved[1]["code"] == "MEDICAL-STANDBY"

    def test_resolve_empty_codes(self):
        tm = TaxonomyManager()
        assert tm.resolve_measure_codes([]) == []


# ===================================================================
# Severity Scoring
# ===================================================================


class TestSeverityScoring:
    """Tests for likelihood x impact -> severity calculation."""

    def test_critical_5x5(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(5, 5) == "critical"

    def test_critical_4x4(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(4, 4) == "critical"

    def test_critical_4x5(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(4, 5) == "critical"

    def test_high_5x2(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(5, 2) == "high"

    def test_high_3x4(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(3, 4) == "high"

    def test_medium_3x2(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(3, 2) == "medium"

    def test_medium_1x5(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(1, 5) == "medium"

    def test_low_2x1(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(2, 1) == "low"

    def test_info_1x1(self):
        tm = TaxonomyManager()
        assert tm.calculate_severity(1, 1) == "info"

    def test_clamps_values(self):
        tm = TaxonomyManager()
        # Values > 5 are clamped to 5
        assert tm.calculate_severity(10, 10) == "critical"
        # Values < 1 are clamped to 1
        assert tm.calculate_severity(0, 0) == "info"

    def test_boundary_score_10(self):
        tm = TaxonomyManager()
        # 2x5 = 10 -> high
        assert tm.calculate_severity(2, 5) == "high"

    def test_boundary_score_16(self):
        tm = TaxonomyManager()
        # 4x4 = 16 -> critical
        assert tm.calculate_severity(4, 4) == "critical"
        # 3x5 = 15 -> high (not critical)
        assert tm.calculate_severity(3, 5) == "high"


# ===================================================================
# Finding Validation
# ===================================================================


class TestFindingValidation:
    """Tests for validate_finding which enriches LLM output."""

    def test_validates_known_class(self):
        tm = TaxonomyManager()
        finding = {
            "risk_class": "FIRE",
            "category": "PHYSICAL",
            "likelihood": 4,
            "impact": 5,
            "description": "Fire scene",
            "recommendation": "Fire dept standby",
            "measure_codes": ["SFX-CLEARANCE", "FIRE-DEPT"],
            "confidence": 0.9,
        }
        result = tm.validate_finding(finding)

        assert result["risk_class"] == "FIRE"
        assert result["rule_id"] == "SEC-P-008"
        assert result["risk_level"] == "critical"  # 4x5=20
        assert len(result["measures"]) == 2
        assert result["measures"][0]["code"] == "SFX-CLEARANCE"

    def test_fills_rule_id_from_class(self):
        tm = TaxonomyManager()
        finding = {
            "risk_class": "HEIGHT",
            "category": "",
            "likelihood": 3,
            "impact": 4,
            "description": "Working at height",
            "recommendation": "Use harness",
        }
        result = tm.validate_finding(finding)

        assert result["rule_id"] == "SEC-P-006"
        assert result["category"] == "PHYSICAL"  # auto-filled
        assert result["risk_level"] == "high"  # 3x4=12

    def test_auto_suggests_measures_when_none_provided(self):
        tm = TaxonomyManager()
        finding = {
            "risk_class": "WATER",
            "category": "PHYSICAL",
            "likelihood": 3,
            "impact": 3,
            "description": "Water scene",
            "recommendation": "Safety divers",
        }
        result = tm.validate_finding(finding)

        # Should auto-suggest measures for WATER class
        codes = [m["code"] for m in result["measures"]]
        assert "WATER-SAFETY" in codes

    def test_handles_unknown_class(self):
        tm = TaxonomyManager()
        finding = {
            "risk_class": "UNKNOWN_RISK",
            "category": "PHYSICAL",
            "likelihood": 2,
            "impact": 3,
            "description": "Some risk",
            "recommendation": "Be careful",
        }
        result = tm.validate_finding(finding)

        assert result["risk_class"] == "UNKNOWN_RISK"
        assert result["rule_id"] == ""
        assert result["risk_level"] == "medium"  # 2x3=6

    def test_clamps_likelihood_impact(self):
        tm = TaxonomyManager()
        finding = {
            "risk_class": "FIRE",
            "category": "PHYSICAL",
            "likelihood": 99,
            "impact": -1,
            "description": "Test",
            "recommendation": "Test",
        }
        result = tm.validate_finding(finding)

        assert result["likelihood"] == 5
        assert result["impact"] == 1
        assert result["risk_level"] == "medium"  # 5x1=5


# ===================================================================
# Prompt Context Generation
# ===================================================================


class TestPromptContext:
    """Tests for summary_for_prompt output."""

    def test_contains_all_categories(self):
        tm = TaxonomyManager()
        ctx = tm.summary_for_prompt()
        assert "PHYSICAL" in ctx
        assert "ENVIRONMENTAL" in ctx
        assert "PSYCHOLOGICAL" in ctx

    def test_contains_rule_ids(self):
        tm = TaxonomyManager()
        ctx = tm.summary_for_prompt()
        assert "SEC-P-001" in ctx
        assert "SEC-E-001" in ctx
        assert "SEC-Y-001" in ctx

    def test_contains_measure_codes(self):
        tm = TaxonomyManager()
        ctx = tm.summary_for_prompt()
        assert "RIG-SAFETY" in ctx
        assert "INTIMACY-COORD" in ctx
        assert "PSY-BRIEFING" in ctx

    def test_contains_scoring_info(self):
        tm = TaxonomyManager()
        ctx = tm.summary_for_prompt()
        assert "likelihood" in ctx.lower()
        assert "impact" in ctx.lower()
        assert "critical" in ctx.lower()


# ===================================================================
# Singleton
# ===================================================================


class TestSingleton:
    """Tests for get_taxonomy_manager caching."""

    def test_returns_same_instance(self):
        # Clear cache first
        get_taxonomy_manager.cache_clear()
        tm1 = get_taxonomy_manager()
        tm2 = get_taxonomy_manager()
        assert tm1 is tm2
