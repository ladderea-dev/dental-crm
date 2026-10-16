"""Unit tests for White-Label Multi-Industry Presets in the standalone template."""

import pytest
import os
import sys

# Ensure local template directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import PIPELINE_PRESETS, apply_pipeline_preset
from prompts import build_prompt, INDUSTRY_KB_PRESETS, DEFAULT_SYSTEM_PROMPT


def test_pipeline_presets_presence():
    """Verify all 5 target industry presets are available."""
    expected_keys = {"aircon", "interior", "dental", "realestate", "builder"}
    assert expected_keys.issubset(set(PIPELINE_PRESETS.keys())), f"Missing presets: {expected_keys - set(PIPELINE_PRESETS.keys())}"


def test_dental_preset_stages():
    """Verify Dental Clinic preset has appropriate clinical stages."""
    dental_stages = [p["name"] for p in PIPELINE_PRESETS["dental"]]
    assert "New Patient Enquiry" in dental_stages
    assert "Symptoms Evaluated" in dental_stages
    assert "Consultation Scheduled" in dental_stages
    assert "Doctor Consultation Done" in dental_stages
    assert "Follow-up Due" in dental_stages


def test_realestate_preset_stages():
    """Verify Real Estate preset has property viewing and booking stages."""
    re_stages = [p["name"] for p in PIPELINE_PRESETS["realestate"]]
    assert "New Lead Received" in re_stages
    assert "Site Visit Scheduled" in re_stages
    assert "Token Advance Paid" in re_stages


def test_builder_preset_stages():
    """Verify Builder preset has launch campaign & milestone stages."""
    b_stages = [p["name"] for p in PIPELINE_PRESETS["builder"]]
    assert "Campaign Enquiry" in b_stages
    assert "Sales Lounge Tour Booked" in b_stages
    assert "Unit Blocked / EOI Submitted" in b_stages
    assert "Possession Handover" in b_stages


def test_prompt_white_labeling():
    """Verify prompt interpolates custom client name and service type without hardcoded vendor references."""
    prompt = build_prompt(
        lead_name="Rahul",
        business_name="Smile Dental Clinic",
        service_type="Root Canal Consultation",
    )
    assert "Smile Dental Clinic" in prompt
    assert "Root Canal Consultation" in prompt
    assert "AirZen" not in prompt, "Found unexpected hardcoded AirZen in generic prompt!"


def test_industry_knowledge_bases():
    """Verify all 5 industry knowledge bases are defined and non-empty."""
    for vertical in ["aircon", "dental", "interior", "realestate", "builder"]:
        assert vertical in INDUSTRY_KB_PRESETS
        assert len(INDUSTRY_KB_PRESETS[vertical]) > 50


def test_branding_endpoint():
    """Verify /api/branding returns configured or default business branding."""
    import asyncio
    from server import api_get_branding

    res = asyncio.run(api_get_branding())
    assert isinstance(res, dict)
    assert "business_name" in res
    assert "business_type" in res
    assert "default_service_type" in res
    assert len(res["business_name"]) > 0


if __name__ == "__main__":
    test_pipeline_presets_presence()
    test_dental_preset_stages()
    test_realestate_preset_stages()
    test_builder_preset_stages()
    test_prompt_white_labeling()
    test_industry_knowledge_bases()
    test_branding_endpoint()
    print("ALL 7 WHITELABEL TESTS PASSED SUCCESSFULLY!")

