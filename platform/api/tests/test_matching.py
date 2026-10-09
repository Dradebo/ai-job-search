"""Synthetic matching coverage across occupations, industries, and countries."""

from __future__ import annotations

import os
import sys
import io
import json
from types import ModuleType
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


os.environ["DATABASE_PATH"] = ":memory:"
os.environ["DOCUMENTS_ROOT"] = "."
os.environ["BOOTSTRAP_EMAIL"] = ""
os.environ["BOOTSTRAP_PASSWORD"] = ""

# The matching tests need no HTTP server. A minimal fallback lets them run in
# source snapshots where the API requirements have not yet been installed.
try:
    import fastapi  # noqa: F401
except ModuleNotFoundError as error:
    if error.name != "fastapi":
        raise

    class _Response:
        def set_cookie(self, *args, **kwargs) -> None:
            pass

        def delete_cookie(self, *args, **kwargs) -> None:
            pass

    class _FastAPI:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def add_middleware(self, *args, **kwargs) -> None:
            pass

        def _route(self, *args, **kwargs):
            return lambda function: function

        get = post = put = delete = _route

    class _HTTPException(Exception):
        pass

    fastapi_module = ModuleType("fastapi")
    fastapi_module.__path__ = []
    fastapi_module.Cookie = lambda *args, **kwargs: None
    fastapi_module.Depends = lambda *args, **kwargs: None
    fastapi_module.FastAPI = _FastAPI
    fastapi_module.File = lambda *args, **kwargs: None
    fastapi_module.HTTPException = _HTTPException
    fastapi_module.Response = _Response
    fastapi_module.UploadFile = type("UploadFile", (), {})
    fastapi_module.status = type("status", (), {"HTTP_401_UNAUTHORIZED": 401})
    middleware_module = ModuleType("fastapi.middleware")
    middleware_module.__path__ = []
    cors_module = ModuleType("fastapi.middleware.cors")
    cors_module.CORSMiddleware = type("CORSMiddleware", (), {})
    responses_module = ModuleType("fastapi.responses")
    responses_module.FileResponse = type("FileResponse", (), {})
    sys.modules.update({
        "fastapi": fastapi_module,
        "fastapi.middleware": middleware_module,
        "fastapi.middleware.cors": cors_module,
        "fastapi.responses": responses_module,
    })

from app.main import (  # noqa: E402
    automated_source_labels,
    configured_greenhouse_sources,
    normalize_listing,
    search_himalayas,
    search_jobicy,
    search_usajobs,
)


def listing(
    *,
    profile: dict,
    title: str,
    company: str,
    location: str,
    body: str,
    role: str,
    provider: str = "Greenhouse: synthetic",
) -> dict | None:
    return normalize_listing(
        provider=provider,
        external_id="synthetic-1",
        title=title,
        company=company,
        url="https://jobs.example.test/role",
        location=location,
        body=body,
        role_queries=[role],
        profile=profile,
    )


class ProfileDrivenMatchingTests(unittest.TestCase):
    def test_public_health_role_matches_in_us_without_admitting_unrelated_ai_role(self) -> None:
        profile = {
            "target_industries": ["Public health"],
            "target_locations": ["United States"],
            "work_mode": "remote",
        }
        match = listing(
            profile=profile,
            title="Public Health Program Manager",
            company="Community Health Partners",
            location="Remote - United States",
            body="Remote public health program management for candidates based in the United States.",
            role="Public Health Program Manager",
        )
        unrelated = listing(
            profile=profile,
            title="Program Manager",
            company="Anthropic",
            location="Remote - United States",
            body="Coordinate research and product delivery for general-purpose AI models.",
            role="Public Health Program Manager",
        )

        self.assertIsNotNone(match)
        self.assertIsNone(unrelated)

    def test_biostatistician_matches_a_health_profile(self) -> None:
        match = listing(
            profile={
                "target_industries": ["Public health", "Healthcare"],
                "target_locations": ["United States"],
                "work_mode": "on-site",
            },
            title="Biostatistician",
            company="State University School of Public Health",
            location="On-site - Baltimore, Maryland, United States",
            body="Apply statistical methods to public health research studies.",
            role="Biostatistician",
        )

        self.assertIsNotNone(match)

    def test_software_role_matches_in_uk_and_respects_work_mode(self) -> None:
        profile = {
            "target_industries": ["Technology"],
            "target_locations": ["London", "United Kingdom"],
            "work_mode": "hybrid",
        }
        match = listing(
            profile=profile,
            title="Senior Software Engineer",
            company="Northstar Systems",
            location="Hybrid - London, UK",
            body="Build software products with a distributed technology team.",
            role="Software Engineer",
        )
        wrong_mode = listing(
            profile={**profile, "work_mode": "remote"},
            title="Senior Software Engineer",
            company="Northstar Systems",
            location="Hybrid - London, UK",
            body="Build software products with a distributed technology team.",
            role="Software Engineer",
        )

        self.assertIsNotNone(match)
        self.assertIsNone(wrong_mode)

    def test_teacher_role_matches_in_kenya(self) -> None:
        match = listing(
            profile={
                "target_industries": ["Education"],
                "target_locations": ["Nairobi", "Kenya"],
                "work_mode": "on-site",
            },
            title="Primary School Teacher",
            company="Nairobi Learning Centre",
            location="On-site - Nairobi, Kenya",
            body="Teach learners in a primary education setting.",
            role="Teacher",
        )

        self.assertIsNotNone(match)

    def test_kampala_it_graduate_matches_uganda_roles_and_location(self) -> None:
        match = listing(
            profile={
                "target_industries": ["Information Technology"],
                "target_locations": ["Kampala", "Uganda"],
                "work_mode": "hybrid",
            },
            title="IT Support Assistant - Graduate",
            company="Kampala Community Services",
            location="Hybrid - Kampala, Uganda",
            body="Provide ICT and computer support to staff. Suitable for a recent IT course graduate.",
            role="IT Support",
        )

        self.assertIsNotNone(match)
        self.assertEqual(match["work_mode"], "hybrid")

    def test_accountant_role_matches_in_canada(self) -> None:
        match = listing(
            profile={
                "target_industries": ["Finance"],
                "target_locations": ["Toronto", "Canada"],
                "work_mode": "hybrid",
            },
            title="Staff Accountant",
            company="Maple Ledger Group",
            location="Hybrid - Toronto, Canada",
            body="Support monthly reporting for a finance team.",
            role="Accountant",
        )

        self.assertIsNotNone(match)

    def test_canada_preference_does_not_treat_california_abbreviation_as_canada(self) -> None:
        match = listing(
            profile={"target_locations": ["Canada"], "work_mode": "any"},
            title="Staff Accountant",
            company="Pacific Ledger Group",
            location="On-site - Sacramento, CA",
            body="Support monthly reporting for a finance team.",
            role="Accountant",
        )

        self.assertIsNone(match)

    def test_explicit_industry_preference_filters_but_is_optional(self) -> None:
        listing_data = {
            "title": "Data Scientist",
            "company": "Anthropic",
            "location": "Remote - United States",
            "body": "Develop general-purpose AI models and machine learning systems.",
            "role": "Data Scientist",
        }
        with_industry = listing(
            profile={"target_industries": ["Public health"], "work_mode": "any"},
            **listing_data,
        )
        without_industry = listing(
            profile={"target_industries": [], "work_mode": "any"},
            **listing_data,
        )

        self.assertIsNone(with_industry)
        self.assertIsNotNone(without_industry)

    def test_public_health_filter_understands_related_occupations_without_loose_word_matches(self) -> None:
        epidemiologist = listing(
            profile={"target_industries": ["Public health"], "work_mode": "any"},
            title="Epidemiologist",
            company="County Department",
            location="Baltimore, Maryland",
            body="Analyze outbreak data and support disease surveillance.",
            role="Epidemiologist",
        )
        unrelated_program_manager = listing(
            profile={"target_industries": ["Public health"], "work_mode": "any"},
            title="Program Manager",
            company="AI Product Lab",
            location="Remote - United States",
            body="Manage a software program and report on product health metrics.",
            role="Program Manager",
        )

        self.assertIsNotNone(epidemiologist)
        self.assertIsNone(unrelated_program_manager)

    @patch.dict(os.environ, {"GREENHOUSE_BOARDS": "cdc, world-bank, cdc, bad/slug"})
    def test_greenhouse_sources_come_only_from_explicit_board_configuration(self) -> None:
        sources = configured_greenhouse_sources()

        self.assertEqual([slug for slug, _ in sources], ["cdc", "world-bank"])
        self.assertTrue(all("boards-api.greenhouse.io/v1/boards/" in url for _, url in sources))

    @patch.dict(os.environ, {"GREENHOUSE_BOARDS": ""})
    @patch("app.main.USAJOBS_USER_AGENT", "")
    @patch("app.main.USAJOBS_API_KEY", "")
    def test_coverage_reports_remote_feeds_only_when_the_search_can_use_them(self) -> None:
        self.assertEqual(automated_source_labels("on-site"), [])
        self.assertEqual(
            automated_source_labels("any"),
            ["Jobicy (remote jobs)", "Himalayas (remote jobs)", "Remotive (remote jobs)"],
        )

    @patch("app.main.urlopen")
    @patch("app.main.USAJOBS_USER_AGENT", "applicant@example.org")
    @patch("app.main.USAJOBS_API_KEY", "synthetic-api-key")
    def test_usajobs_adapter_uses_credentials_and_normalizes_federal_listings(self, urlopen) -> None:
        payload = {
            "SearchResult": {
                "SearchResultItems": [{
                    "MatchedObjectId": "12345",
                    "MatchedObjectDescriptor": {
                        "PositionID": "CDC-12345",
                        "PositionTitle": "Biostatistician",
                        "PositionURI": "https://www.usajobs.gov/job/12345",
                        "ApplyURI": ["https://www.usajobs.gov/apply/12345"],
                        "OrganizationName": "Centers for Disease Control and Prevention",
                        "PositionLocation": [{"LocationName": "Atlanta, Georgia"}],
                        "QualificationSummary": "Support public health research using statistical methods.",
                    },
                }],
            },
        }
        urlopen.return_value = io.BytesIO(json.dumps(payload).encode("utf-8"))

        matches = search_usajobs(
            "Biostatistician", "", "any",
            {"target_industries": ["Public health"], "target_locations": ["United States"], "work_mode": "any"},
            ["Biostatistician"],
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], "USAJOBS")
        self.assertIn("Atlanta, Georgia", matches[0]["location"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization-key"), "synthetic-api-key")
        self.assertEqual(request.get_header("User-agent"), "applicant@example.org")
        parameters = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(parameters["WhoMayApply"], ["public"])

    def test_no_location_preference_does_not_impose_a_us_only_filter(self) -> None:
        match = listing(
            profile={"target_roles": ["Teacher"], "work_mode": "any"},
            title="Primary School Teacher",
            company="Nairobi Learning Centre",
            location="On-site - Nairobi, Kenya",
            body="Teach learners in a primary education setting.",
            role="Teacher",
        )

        self.assertIsNotNone(match)

    def test_legacy_remote_us_preference_still_requires_us_eligibility(self) -> None:
        profile = {"target_locations": ["United States"], "work_mode": "remote-US"}
        us_match = listing(
            profile=profile,
            title="Software Engineer",
            company="Northstar Systems",
            location="Remote - United States",
            body="Remote position for candidates based in the United States.",
            role="Software Engineer",
            provider="Remotive",
        )
        uk_listing = listing(
            profile=profile,
            title="Software Engineer",
            company="Northstar Systems",
            location="Remote - United Kingdom",
            body="Remote position for candidates based in the United Kingdom.",
            role="Software Engineer",
            provider="Remotive",
        )

        self.assertIsNotNone(us_match)
        self.assertIsNone(uk_listing)

    @patch("app.main.fetch_json", return_value={"jobs": []})
    def test_jobicy_search_does_not_force_us_geography(self, fetch_json) -> None:
        search_jobicy("Teacher", "", "remote", {}, ["Teacher"])
        parameters = parse_qs(urlparse(fetch_json.call_args.args[0]).query)

        self.assertNotIn("geo", parameters)

    @patch("app.main.fetch_json", return_value={"jobs": []})
    def test_himalayas_search_does_not_force_us_country(self, fetch_json) -> None:
        search_himalayas("Teacher", "Nairobi, Kenya", "remote", {}, ["Teacher"])
        parameters = parse_qs(urlparse(fetch_json.call_args.args[0]).query)

        self.assertNotIn("country", parameters)
        self.assertEqual(parameters["worldwide"], ["true"])


if __name__ == "__main__":
    unittest.main()

