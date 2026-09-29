from __future__ import annotations

import hashlib
import html
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/app.db")
Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
DOCUMENTS_ROOT = Path(os.getenv("DOCUMENTS_ROOT", "./data/documents"))
DOCUMENTS_ROOT.mkdir(parents=True, exist_ok=True)
MAX_DOCUMENT_BYTES = int(os.getenv("MAX_DOCUMENT_BYTES", str(15 * 1024 * 1024)))
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".png", ".jpg", ".jpeg"}
APP_ORIGINS = [
    origin.strip()
    for origin in os.getenv("APP_ORIGIN", "http://localhost:3000").split(",")
    if origin.strip()
]
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")
BOOTSTRAP_EMAIL = os.getenv("BOOTSTRAP_EMAIL", "").strip().lower()
BOOTSTRAP_PASSWORD = os.getenv("BOOTSTRAP_PASSWORD", "")
AI_JOB_SEARCH_ROOT = Path(os.getenv("AI_JOB_SEARCH_ROOT", "/engines/ai-job-search"))
CAREER_OPS_ROOT = Path(os.getenv("CAREER_OPS_ROOT", "/engines/career-ops"))
DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "6"))
DISCOVERY_MAX_RESULTS = int(os.getenv("DISCOVERY_MAX_RESULTS", "60"))
GREENHOUSE_MAX_BOARDS = int(os.getenv("GREENHOUSE_MAX_BOARDS", "12"))

db_lock = threading.Lock()
db = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    with db_lock:
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                profile_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                work_mode TEXT NOT NULL DEFAULT 'unknown',
                authorization TEXT NOT NULL DEFAULT 'unclear',
                fit TEXT NOT NULL DEFAULT 'unreviewed',
                notes TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                external_id TEXT NOT NULL DEFAULT '',
                fit_score REAL,
                archived INTEGER NOT NULL DEFAULT 0,
                health_context INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'saved',
                notes TEXT NOT NULL DEFAULT '',
                application_url TEXT NOT NULL DEFAULT '',
                contact_email TEXT NOT NULL DEFAULT '',
                email_subject TEXT NOT NULL DEFAULT '',
                email_body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        ensure_column("applications", "application_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column("applications", "contact_email", "TEXT NOT NULL DEFAULT ''")
        ensure_column("applications", "email_subject", "TEXT NOT NULL DEFAULT ''")
        ensure_column("applications", "email_body", "TEXT NOT NULL DEFAULT ''")
        ensure_column("jobs", "source", "TEXT NOT NULL DEFAULT ''")
        ensure_column("jobs", "external_id", "TEXT NOT NULL DEFAULT ''")
        ensure_column("jobs", "fit_score", "REAL")
        ensure_column("jobs", "archived", "INTEGER NOT NULL DEFAULT 0")
        ensure_column("jobs", "health_context", "INTEGER NOT NULL DEFAULT 0")
        db.commit()

        if BOOTSTRAP_EMAIL and BOOTSTRAP_PASSWORD:
            existing = db.execute("SELECT id FROM users LIMIT 1").fetchone()
            if existing is None:
                user_id = db.execute(
                    "INSERT INTO users (email, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (BOOTSTRAP_EMAIL, hash_password(BOOTSTRAP_PASSWORD), BOOTSTRAP_EMAIL.split("@")[0], utc_now()),
                ).lastrowid
                db.execute(
                    "INSERT INTO profiles (user_id, profile_json, updated_at) VALUES (?, ?, ?)",
                    (user_id, json.dumps({"name": BOOTSTRAP_EMAIL.split("@")[0]}), utc_now()),
                )
                db.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(derived.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def ensure_column(table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(session: str | None) -> sqlite3.Row:
    if not session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    with db_lock:
        row = db.execute(
            """
            SELECT u.* FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (session_hash(session), utc_now()),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return row


def user_dependency(session: str | None = Cookie(default=None)) -> sqlite3.Row:
    return current_user(session)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)


class ProfileUpdate(BaseModel):
    name: str = Field(default="", max_length=160)
    phone: str = Field(default="", max_length=60)
    address: str = Field(default="", max_length=500)
    linkedin: str = Field(default="", max_length=500)
    website: str = Field(default="", max_length=500)
    target_roles: list[str] = Field(default_factory=list, max_length=12)
    target_industries: list[str] = Field(default_factory=list, max_length=12)
    target_locations: list[str] = Field(default_factory=list, max_length=12)
    work_mode: str = Field(default="hybrid", max_length=40)
    authorization: str = Field(default="unclear", max_length=80)
    relocation: str = Field(default="case_by_case", max_length=40)
    commute_minutes: int | None = Field(default=None, ge=0, le=600)
    summary: str = Field(default="", max_length=5000)
    skills: str = Field(default="", max_length=5000)
    experience: str = Field(default="", max_length=10000)
    education: str = Field(default="", max_length=5000)
    licenses: str = Field(default="", max_length=5000)
    salary_floor: str = Field(default="", max_length=120)


class JobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    company: str = Field(min_length=1, max_length=240)
    url: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=240)
    work_mode: str = Field(default="unknown", max_length=40)
    authorization: str = Field(default="unclear", max_length=80)
    fit: str = Field(default="unreviewed", max_length=40)
    notes: str = Field(default="", max_length=5000)


class ApplicationCreate(BaseModel):
    job_id: int | None = None
    status: str = Field(default="saved", max_length=40)
    notes: str = Field(default="", max_length=5000)
    application_url: str = Field(default="", max_length=2000)
    contact_email: str = Field(default="", max_length=320)
    email_subject: str = Field(default="", max_length=500)
    email_body: str = Field(default="", max_length=20000)


class ApplicationUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=40)
    notes: str | None = Field(default=None, max_length=5000)
    application_url: str | None = Field(default=None, max_length=2000)
    contact_email: str | None = Field(default=None, max_length=320)
    email_subject: str | None = Field(default=None, max_length=500)
    email_body: str | None = Field(default=None, max_length=20000)


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=1000)
    location: str = Field(default="", max_length=160)
    work_mode: str = Field(default="any", max_length=40)


STOP_WORDS = {
    "a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with",
    "job", "jobs", "role", "roles", "work", "working",
}

GENERIC_ROLE_WORDS = STOP_WORDS | {
    "associate", "entry", "graduate", "junior", "lead", "mid", "principal",
    "senior", "staff",
}

US_STATES_AND_TERRITORIES = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire",
    "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma",
    "Oregon", "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee",
    "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "District of Columbia", "Puerto Rico", "Guam", "U.S. Virgin Islands", "American Samoa",
    "Northern Mariana Islands",
)
US_STATE_CODES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def search_terms(query: str) -> list[str]:
    return list(dict.fromkeys(
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9+#.\-/]{1,}", query)
        if token.lower() not in STOP_WORDS
    ))


def split_role_queries(query: str) -> list[str]:
    """Treat the comma-separated profile roles as separate search phrases."""
    roles = [clean_text(part) for part in re.split(r"[,;|\n]+", query) if clean_text(part)]
    return list(dict.fromkeys(roles))[:12]


def normalized_words(value: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", clean_text(value).lower()).strip()


def contains_phrase(text: str, phrase: str) -> bool:
    normalized_text = normalized_words(text)
    normalized_phrase = normalized_words(phrase)
    return bool(normalized_phrase and f" {normalized_phrase} " in f" {normalized_text} ")


def role_matches_title(role: str, title: str, body: str = "", company: str = "") -> bool:
    """Match any occupation using title evidence, not a single hard-coded industry."""
    role_text = normalized_words(role)
    title_text = normalized_words(title)
    if not role_text or not title_text:
        return False
    if contains_phrase(title, role):
        return True

    role_terms = [term for term in search_terms(role_text) if term not in GENERIC_ROLE_WORDS]
    if not role_terms:
        return False
    title_terms = set(search_terms(title_text))
    listing_terms = set(search_terms(f"{title} {body} {company}"))
    title_hits = sum(term in title_terms for term in role_terms)
    listing_hits = sum(term in listing_terms for term in role_terms)
    required_title_hits = 1
    required_listing_hits = 1 if len(role_terms) == 1 else max(2, (len(role_terms) * 2 + 2) // 3)
    return title_hits >= required_title_hits and listing_hits >= required_listing_hits


COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "united states": ("united states", "united states of america", "usa", "us"),
    "united kingdom": ("united kingdom", "great britain", "britain", "uk", "gb"),
    "canada": ("canada",),
    "australia": ("australia",),
    "kenya": ("kenya",),
    "uganda": ("uganda",),
    "south africa": ("south africa",),
    "nigeria": ("nigeria",),
    "ghana": ("ghana",),
    "india": ("india",),
    "ireland": ("ireland",),
    "germany": ("germany",),
    "france": ("france",),
    "netherlands": ("netherlands", "the netherlands"),
    "philippines": ("philippines",),
    "singapore": ("singapore",),
    "new zealand": ("new zealand",),
}
COUNTRY_ALIAS_TO_CANONICAL = {
    alias: country for country, aliases in COUNTRY_ALIASES.items() for alias in aliases
}
US_STATE_CODE_BY_NAME = {
    state.lower(): code
    for state, code in zip(US_STATES_AND_TERRITORIES[:len(US_STATE_CODES)], US_STATE_CODES)
}
US_COUNTRY_NAMES = set(COUNTRY_ALIASES["united states"])


def location_preferences(profile: dict[str, Any]) -> list[str]:
    raw = profile.get("target_locations", [])
    if isinstance(raw, str):
        raw = re.split(r"[,;|\n]+", raw)
    return [clean_text(value) for value in raw if clean_text(value)] if isinstance(raw, list) else []


def country_canonical(value: str) -> str | None:
    normalized = normalized_words(value)
    return COUNTRY_ALIAS_TO_CANONICAL.get(normalized)


def country_is_eligible(location: str, body: str, country: str) -> bool:
    aliases = COUNTRY_ALIASES.get(country, (country,))
    location_text = normalized_words(location)
    body_text = normalized_words(body)
    if any(contains_phrase(location_text, alias) for alias in aliases):
        return True
    return any(
        re.search(rf"\b(?:remote|based|located|reside\w*|candidates?\s+(?:must|can|may)\s+be)\b.{{0,60}}\b{re.escape(normalized_words(alias))}\b", body_text)
        for alias in aliases
        if normalized_words(alias)
    )


def is_location_eligible(location: str, body: str, profile: dict[str, Any]) -> bool:
    """Treat an empty location preference as unrestricted; otherwise require evidence."""
    preferences = location_preferences(profile)
    if not preferences:
        return True

    location_text = clean_text(location)
    body_text = clean_text(body)
    combined = f"{location_text} {body_text}"
    remote_listing = bool(re.search(r"\b(remote|work from home|telecommut\w*)\b", combined, re.I))
    worldwide_listing = bool(re.search(r"\b(worldwide|global|anywhere in the world|all countries)\b", combined, re.I))
    listing_is_us_eligible = is_us_eligible(location_text, body_text)

    for preference in preferences:
        preference_text = normalized_words(preference)
        if preference_text in {"anywhere", "worldwide", "global"} and worldwide_listing:
            return True
        if preference_text in {"remote", "remote work"} and remote_listing:
            return True
        if preference_text in {"remote us", "remote usa", "remote united states"}:
            if remote_listing and listing_is_us_eligible:
                return True
            continue

        canonical = country_canonical(preference)
        if canonical == "united states":
            if listing_is_us_eligible:
                return True
            continue
        if canonical and country_is_eligible(location_text, body_text, canonical):
            return True

        # A state, city, region, or unlisted country may be supplied in free text.
        if contains_phrase(location_text, preference):
            return True
        state_code = US_STATE_CODE_BY_NAME.get(preference_text)
        if state_code and contains_phrase(location_text, state_code):
            return True
        if remote_listing and is_us_eligible(preference, "") and listing_is_us_eligible:
            return True
        if worldwide_listing and remote_listing:
            return True

    return False


def industry_matches(profile: dict[str, Any], title: str, body: str, company: str) -> bool:
    industries = profile.get("target_industries", [])
    if not isinstance(industries, list) or not industries:
        return True
    listing_text = normalized_words(f"{title} {body} {company}")
    for industry in industries:
        terms = search_terms(clean_text(industry))
        if contains_phrase(listing_text, industry):
            return True
        if terms and len(terms) > 1 and sum(term in set(search_terms(listing_text)) for term in terms) >= (len(terms) + 1) // 2:
            return True
        if terms and len(terms) == 1 and terms[0] in set(search_terms(listing_text)):
            return True
    return False


def work_mode_matches(requested: str, inferred: str, location: str, body: str) -> bool:
    requested_mode = clean_text(requested).lower().replace("_", "-")
    inferred_mode = clean_text(inferred).lower().replace("_", "-")
    if requested_mode in {"", "any", "unknown"}:
        return True
    if requested_mode in {"remote-us", "remote usa", "remote in the us"}:
        return inferred_mode in {"remote", "remote-us"} and is_us_eligible(location, body)
    if requested_mode == "remote":
        return inferred_mode in {"remote", "remote-us"}
    if requested_mode in {"onsite", "on site"}:
        requested_mode = "on-site"
    return inferred_mode == requested_mode


def is_us_eligible(location: str, body: str = "") -> bool:
    location_text = clean_text(location).lower()
    if re.search(r"\b(united states|u\.?s\.?a?\.?|usa)\b", location_text):
        return True
    if re.search(r"\b(worldwide|global|anywhere in the world|all countries)\b", location_text):
        return True
    if any(re.search(rf"\b{re.escape(state.lower())}\b", location_text) for state in US_STATES_AND_TERRITORIES):
        return True
    if re.search(r",\s*(?:" + "|".join(US_STATE_CODES) + r")\b", location_text, re.I):
        return True
    body_text = clean_text(body).lower()
    return bool(re.search(
        r"(?:remote|based|located|reside\w*).{0,45}\b(?:in|within|from)?\s*(?:the\s+)?(?:united states|u\.?s\.?a?\.?|usa)\b|"
        r"\b(?:must be|candidates? (?:must|should) be|employees? (?:must|can) be).{0,45}\b(?:based|located|resident|remote).{0,35}\b(?:united states|u\.?s\.?a?\.?|usa)\b",
        body_text,
    ))


def infer_work_mode(provider: str, location: str, body: str) -> str:
    if provider in {"Jobicy", "Himalayas", "Remotive"}:
        return "remote-US" if is_us_eligible(location, body) else "remote"
    text = f"{clean_text(location)} {clean_text(body[:3000])}".lower()
    if re.search(r"\bhybrid\b", text):
        return "hybrid"
    if re.search(r"\b(remote|work from home|telecommut\w*)\b", text):
        return "remote-US" if is_us_eligible(location, body) else "remote"
    if re.search(r"\b(on[- ]?site|in[- ]person|office[- ]based)\b", text):
        return "on-site"
    return "unknown"


def fetch_json(url: str) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "MumsJobDesk/0.2 (+https://nextjs-boilerplate-dradebos-projects.vercel.app/)",
        },
    )
    with urlopen(request, timeout=DISCOVERY_TIMEOUT_SECONDS) as response:
        return json.load(response)


def configured_greenhouse_sources() -> list[tuple[str, str]]:
    config_path = CAREER_OPS_ROOT / "templates" / "portals.example.yml"
    try:
        config = config_path.read_text(encoding="utf-8")
    except OSError:
        return []
    sources: list[tuple[str, str]] = []
    seen: set[str] = set()
    for api_url, token in re.findall(
        r"api:\s+(https://boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([^/]+)/jobs)",
        config,
    ):
        if api_url not in seen:
            sources.append((token, api_url + "?content=true"))
            seen.add(api_url)
    return sources[:GREENHOUSE_MAX_BOARDS]


def score_listing(title: str, body: str, matched_role: str, profile: dict[str, Any]) -> tuple[float, str, str]:
    terms = [term for term in search_terms(matched_role) if term not in GENERIC_ROLE_WORDS]
    title_text = clean_text(title).lower()
    body_text = clean_text(body).lower()
    skill_text = clean_text(profile.get("skills", "")).lower()
    body_hits = sum(1 for term in terms if term in body_text)
    skill_terms = [term for term in search_terms(skill_text) if term not in GENERIC_ROLE_WORDS and len(term) >= 4]
    skill_hits = sum(1 for term in set(skill_terms) if term in title_text or term in body_text)
    exact_phrase = clean_text(matched_role).lower() in title_text
    score = 58.0 + (14.0 if exact_phrase else 7.0) + min(10.0, body_hits * 2.0) + min(6.0, skill_hits * 2.0)
    fit = "strong" if score >= 78 else "possible"
    reason = f"title matches {matched_role}"
    if skill_hits:
        reason += "; profile skills appear in the posting"
    if profile.get("target_industries"):
        reason += "; posting matches a selected industry"
    if location_preferences(profile):
        reason += "; posting location is compatible with your preferences"
    return round(min(score, 100.0), 1), fit, reason


def normalize_listing(
    *,
    provider: str,
    external_id: Any,
    title: Any,
    company: Any,
    url: Any,
    location: Any,
    body: Any,
    role_queries: list[str],
    profile: dict[str, Any],
) -> dict[str, Any] | None:
    title_text = clean_text(title)
    company_text = clean_text(company) or "Company not stated"
    url_text = str(url or "").strip()
    if not title_text or not url_text:
        return None
    body_text = clean_text(body)
    matched_role = next((
        role for role in role_queries
        if role_matches_title(role, title_text, body_text, company_text)
    ), None)
    listing_location = clean_text(location)
    inferred_mode = infer_work_mode(provider, listing_location, body_text)
    if (
        not matched_role
        or not industry_matches(profile, title_text, body_text, company_text)
        or not is_location_eligible(listing_location, body_text, profile)
        or not work_mode_matches(profile.get("work_mode", "any"), inferred_mode, listing_location, body_text)
    ):
        return None
    fit_score, fit, reason = score_listing(title_text, body_text, matched_role, profile)
    if not listing_location:
        listing_location = (
            "Remote (location restrictions not listed)"
            if provider in {"Jobicy", "Himalayas", "Remotive"}
            else "Location not stated"
        )
    return {
        "title": title_text[:240],
        "company": company_text[:240],
        "url": url_text[:2000],
        "location": listing_location[:240],
        "work_mode": inferred_mode,
        "authorization": "unclear",
        "fit": fit,
        "fit_score": fit_score,
        # Kept for existing database compatibility; matching is domain-neutral.
        "health_context": 0,
        "source": provider,
        "external_id": str(external_id or url_text),
        "notes": f"Source: {provider}. Match note: {reason}. Human review required before any application.",
    }


def search_jobicy(
    query: str,
    location: str,
    work_mode: str,
    profile: dict[str, Any],
    target_roles: list[str] | None = None,
) -> list[dict[str, Any]]:
    if work_mode in {"on-site", "onsite"}:
        return []
    params = {"count": "50", "tag": query[:50]}
    data = fetch_json("https://jobicy.com/api/v2/remote-jobs?" + urlencode(params))
    results = []
    for raw in data.get("jobs", []):
        results.append(normalize_listing(
            provider="Jobicy", external_id=raw.get("id"), title=raw.get("jobTitle"),
            company=raw.get("companyName"), url=raw.get("url"), location=raw.get("jobGeo"),
            body=" ".join([raw.get("jobExcerpt", ""), raw.get("jobDescription", ""), " ".join(raw.get("jobIndustry", []))]),
            role_queries=target_roles or [query], profile=profile,
        ))
    return [result for result in results if result]


def search_himalayas(
    query: str,
    location: str,
    work_mode: str,
    profile: dict[str, Any],
    target_roles: list[str] | None = None,
) -> list[dict[str, Any]]:
    if work_mode in {"on-site", "onsite"}:
        return []
    params = {"q": query[:120], "worldwide": "true", "page": "1"}
    data = fetch_json("https://himalayas.app/jobs/api/search?" + urlencode(params))
    results = []
    for raw in data.get("jobs", []):
        restrictions = raw.get("locationRestrictions") or []
        results.append(normalize_listing(
            provider="Himalayas", external_id=raw.get("guid"), title=raw.get("title"),
            company=raw.get("companyName"), url=raw.get("applicationLink") or raw.get("guid"),
            location=", ".join(clean_text(item) for item in restrictions),
            body=" ".join([raw.get("excerpt", ""), raw.get("description", ""), " ".join(raw.get("categories", []))]),
            role_queries=target_roles or [query], profile=profile,
        ))
    return [result for result in results if result]


def search_remotive(
    query: str,
    location: str,
    work_mode: str,
    profile: dict[str, Any],
    target_roles: list[str] | None = None,
) -> list[dict[str, Any]]:
    if work_mode in {"on-site", "onsite"}:
        return []
    params = {"search": query[:120], "limit": "50"}
    data = fetch_json("https://remotive.com/api/remote-jobs?" + urlencode(params))
    results = []
    for raw in data.get("jobs", []):
        results.append(normalize_listing(
            provider="Remotive", external_id=raw.get("id"), title=raw.get("title"),
            company=raw.get("company_name"), url=raw.get("url"), location=raw.get("candidate_required_location"),
            body=" ".join([raw.get("description", ""), " ".join(raw.get("tags", []))]),
            role_queries=target_roles or [query], profile=profile,
        ))
    return [result for result in results if result]


def search_greenhouse_board(source: tuple[str, str], role_queries: list[str], profile: dict[str, Any]) -> list[dict[str, Any]]:
    token, url = source
    data = fetch_json(url)
    results = []
    for raw in data.get("jobs", []):
        location_name = (raw.get("location") or {}).get("name", "")
        results.append(normalize_listing(
            provider=f"Greenhouse: {token}", external_id=raw.get("id"), title=raw.get("title"),
            company=token.replace("-", " ").title(), url=raw.get("absolute_url"), location=location_name,
            body=" ".join([raw.get("content", ""), raw.get("title", "")]),
            role_queries=role_queries, profile=profile,
        ))
    return [result for result in results if result]


def discover_jobs(role_queries: list[str], location: str, work_mode: str, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    search_profile = dict(profile)
    if location.strip():
        search_profile["target_locations"] = [clean_text(item) for item in re.split(r"[,;|\n]+", location) if clean_text(item)]
    if work_mode != "any":
        search_profile["work_mode"] = work_mode
    elif search_profile.get("work_mode") in {None, ""}:
        search_profile["work_mode"] = "any"
    tasks = {}
    for role in role_queries:
        tasks[f"Jobicy:{role}"] = lambda role=role: search_jobicy(role, location, work_mode, search_profile, role_queries)
        tasks[f"Himalayas:{role}"] = lambda role=role: search_himalayas(role, location, work_mode, search_profile, role_queries)
        tasks[f"Remotive:{role}"] = lambda role=role: search_remotive(role, location, work_mode, search_profile, role_queries)
    sources = configured_greenhouse_sources()
    for source in sources:
        tasks[f"Greenhouse:{source[0]}"] = lambda source=source: search_greenhouse_board(source, role_queries, search_profile)
    results: list[dict[str, Any]] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(tasks)))) as executor:
        futures = {executor.submit(task): name for name, task in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results.extend(future.result())
            except Exception as error:
                warnings.append(f"{name} was unavailable: {type(error).__name__}")
    deduplicated: dict[str, dict[str, Any]] = {}
    for result in results:
        previous = deduplicated.get(result["url"])
        if previous is None or result["fit_score"] > previous["fit_score"]:
            deduplicated[result["url"]] = result
    results = sorted(deduplicated.values(), key=lambda item: (-item["fit_score"], item["company"], item["title"]))
    return results[:DISCOVERY_MAX_RESULTS], warnings


def persist_discovered_jobs(user_id: int, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    persisted: list[dict[str, Any]] = []
    with db_lock:
        for match in matches:
            existing = db.execute("SELECT id FROM jobs WHERE user_id = ? AND url = ?", (user_id, match["url"])).fetchone()
            values = (
                match["title"], match["company"], match["url"], match["location"], match["work_mode"],
                match["authorization"], match["fit"], match["notes"], match["source"], match["external_id"],
                match["fit_score"], match["health_context"],
            )
            if existing:
                db.execute(
                    "UPDATE jobs SET title=?, company=?, location=?, work_mode=?, authorization=?, fit=?, notes=?, source=?, external_id=?, fit_score=?, health_context=?, archived=0 WHERE id=? AND user_id=?",
                    (*values[:2], *values[3:], existing["id"], user_id),
                )
                job_id = existing["id"]
            else:
                cursor = db.execute(
                    "INSERT INTO jobs (user_id, title, company, url, location, work_mode, authorization, fit, notes, source, external_id, fit_score, health_context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, *values, utc_now()),
                )
                job_id = cursor.lastrowid
            row = db.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
            persisted.append(row_to_job(row))
        db.commit()
    return persisted


def archive_irrelevant_discoveries(user_id: int, role_queries: list[str], profile: dict[str, Any]) -> int:
    """Hide stale machine-found jobs without deleting jobs or application history."""
    with db_lock:
        rows = db.execute(
            """
            SELECT j.id, j.title, j.company, j.location, j.work_mode, j.health_context FROM jobs j
            WHERE j.user_id = ? AND j.source <> '' AND j.archived = 0
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
            """,
            (user_id,),
        ).fetchall()
        to_archive = [
            row["id"] for row in rows
            if not any(role_matches_title(
                role, row["title"], company=row["company"]
            ) for role in role_queries)
            or not industry_matches(profile, row["title"], "", row["company"])
            or not is_location_eligible(row["location"], "", profile)
            or not work_mode_matches(
                profile.get("work_mode", "any"), row["work_mode"], row["location"], ""
            )
        ]
        if to_archive:
            placeholders = ",".join("?" for _ in to_archive)
            db.execute(
                f"UPDATE jobs SET archived = 1 WHERE user_id = ? AND id IN ({placeholders})",
                [user_id, *to_archive],
            )
            db.commit()
    return len(to_archive)


init_db()
app = FastAPI(title="AI Job Search API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=APP_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "ai-job-search-api"}


@app.post("/api/v1/auth/login")
def login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    with db_lock:
        user = db.execute("SELECT * FROM users WHERE email = ?", (payload.email.strip().lower(),)).fetchone()
    if user is None or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email or password is incorrect")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    with db_lock:
        db.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (session_hash(token), user["id"], expires_at.isoformat()),
        )
        db.commit()
    response.set_cookie(
        "session",
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        max_age=SESSION_DAYS * 86400,
    )
    return {"user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"]}}


@app.post("/api/v1/auth/logout")
def logout(response: Response, session: str | None = Cookie(default=None)) -> dict[str, bool]:
    if session:
        with db_lock:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (session_hash(session),))
            db.commit()
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/v1/me")
def me(user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        profile = db.execute("SELECT profile_json FROM profiles WHERE user_id = ?", (user["id"],)).fetchone()
    return {
        "user": {"id": user["id"], "email": user["email"], "display_name": user["display_name"]},
        "profile": json.loads(profile["profile_json"]) if profile else {},
    }


@app.get("/api/v1/profile")
def get_profile(user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    return me(user)


@app.put("/api/v1/profile")
def update_profile(payload: ProfileUpdate, user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    profile = payload.model_dump()
    with db_lock:
        db.execute(
            "INSERT INTO profiles (user_id, profile_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at",
            (user["id"], json.dumps(profile), utc_now()),
        )
        db.execute("UPDATE users SET display_name = ? WHERE id = ?", (profile["name"], user["id"]))
        db.commit()
    return {"profile": profile}


def document_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["original_name"],
        "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"],
    }


def owned_document(document_id: int, user_id: int) -> sqlite3.Row:
    with db_lock:
        row = db.execute(
            "SELECT * FROM documents WHERE id = ? AND user_id = ?",
            (document_id, user_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return row


@app.get("/api/v1/documents")
def list_documents(user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        rows = db.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return {"documents": [document_to_dict(row) for row in rows]}


@app.post("/api/v1/documents")
async def upload_document(
    upload: UploadFile = File(...),
    user: sqlite3.Row = Depends(user_dependency),
) -> dict[str, Any]:
    original_name = Path(upload.filename or "").name.strip()
    extension = Path(original_name).suffix.lower()
    if not original_name or extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Use a PDF, Word document, text file, or image.")

    user_directory = DOCUMENTS_ROOT / str(user["id"])
    user_directory.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}{extension}"
    stored_path = user_directory / stored_name
    size = 0
    try:
        with stored_path.open("wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise HTTPException(status_code=413, detail="That file is larger than the 15 MB limit.")
                destination.write(chunk)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    created_at = utc_now()
    with db_lock:
        cursor = db.execute(
            "INSERT INTO documents (user_id, original_name, stored_name, content_type, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], original_name, stored_name, upload.content_type or "application/octet-stream", size, created_at),
        )
        db.commit()
        row = db.execute("SELECT * FROM documents WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"document": document_to_dict(row)}


@app.get("/api/v1/documents/{document_id}")
def download_document(document_id: int, user: sqlite3.Row = Depends(user_dependency)) -> FileResponse:
    row = owned_document(document_id, user["id"])
    path = DOCUMENTS_ROOT / str(user["id"]) / row["stored_name"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Document file is missing")
    return FileResponse(path, media_type=row["content_type"], filename=row["original_name"])


@app.delete("/api/v1/documents/{document_id}")
def delete_document(document_id: int, user: sqlite3.Row = Depends(user_dependency)) -> dict[str, bool]:
    row = owned_document(document_id, user["id"])
    path = DOCUMENTS_ROOT / str(user["id"]) / row["stored_name"]
    path.unlink(missing_ok=True)
    with db_lock:
        db.execute("DELETE FROM documents WHERE id = ? AND user_id = ?", (document_id, user["id"]))
        db.commit()
    return {"ok": True}


@app.post("/api/v1/search")
def search(payload: SearchRequest, user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        profile_row = db.execute("SELECT profile_json FROM profiles WHERE user_id = ?", (user["id"],)).fetchone()
    profile = json.loads(profile_row["profile_json"]) if profile_row else {}
    query = payload.query.strip() or ", ".join(profile.get("target_roles", []))
    location = payload.location.strip() or ", ".join(profile.get("target_locations", []))
    work_mode = payload.work_mode if payload.work_mode != "any" else profile.get("work_mode", "any")
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="Add at least one target role or skill before searching")
    role_queries = split_role_queries(query)
    search_profile = dict(profile)
    if location:
        search_profile["target_locations"] = [clean_text(item) for item in re.split(r"[,;|\n]+", location) if clean_text(item)]
    search_profile["work_mode"] = work_mode
    search_query = " ".join(part for part in [query, location, work_mode if work_mode != "any" else ""] if part)
    all_roles_query = " OR ".join(f'"{role}"' for role in role_queries)
    encoded = quote_plus(f"({all_roles_query}) {location} jobs")
    matches, warnings = discover_jobs(role_queries, location, work_mode, profile)
    persisted = persist_discovered_jobs(user["id"], matches)
    archive_irrelevant_discoveries(user["id"], role_queries, search_profile)
    return {
        "provider": "public-job-feeds-and-career-ops-greenhouse",
        "query": search_query,
        "message": "Results are filtered against your selected roles, industries, locations, and work preference. Check each employer's eligibility requirements before preparing an application.",
        "matches": persisted,
        "warnings": warnings,
        "engine_checks": {
            "career_ops": (CAREER_OPS_ROOT / "templates" / "portals.example.yml").is_file(),
            "ai_job_search": (AI_JOB_SEARCH_ROOT / ".claude" / "skills" / "job-scraper" / "SKILL.md").is_file(),
        },
        "links": [
            {"source": "Google Jobs search — all target roles", "url": f"https://www.google.com/search?q={encoded}"},
            {"source": "LinkedIn Jobs — all target roles", "url": f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(all_roles_query)}&location={quote_plus(location)}"},
            *([
                {"source": "USAJOBS — all target roles", "url": f"https://www.usajobs.gov/Search/Results?keyword={quote_plus(' OR '.join(role_queries))}&l={quote_plus(location)}"},
            ] if any(is_us_eligible(preference, "") for preference in location_preferences(search_profile)) else []),
        ],
    }


def row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


@app.get("/api/v1/jobs")
def list_jobs(user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        rows = db.execute(
            """
            SELECT * FROM jobs WHERE user_id = ?
              AND (archived = 0 OR EXISTS (SELECT 1 FROM applications a WHERE a.job_id = jobs.id))
            ORDER BY created_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"jobs": [row_to_job(row) for row in rows]}


@app.post("/api/v1/jobs")
def create_job(payload: JobCreate, user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        cursor = db.execute(
            "INSERT INTO jobs (user_id, title, company, url, location, work_mode, authorization, fit, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], payload.title, payload.company, payload.url, payload.location, payload.work_mode, payload.authorization, payload.fit, payload.notes, utc_now()),
        )
        db.commit()
        row = db.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return {"job": row_to_job(row)}


@app.get("/api/v1/applications")
def list_applications(user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    with db_lock:
        rows = db.execute(
            """
            SELECT a.*, j.title, j.company, j.location, j.url
            FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
            WHERE a.user_id = ? ORDER BY a.updated_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"applications": [dict(row) for row in rows]}


@app.post("/api/v1/applications")
def create_application(payload: ApplicationCreate, user: sqlite3.Row = Depends(user_dependency)) -> dict[str, Any]:
    if payload.job_id is not None:
        with db_lock:
            job = db.execute("SELECT id FROM jobs WHERE id = ? AND user_id = ?", (payload.job_id, user["id"])).fetchone()
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
    now = utc_now()
    with db_lock:
        cursor = db.execute(
            "INSERT INTO applications (user_id, job_id, status, notes, application_url, contact_email, email_subject, email_body, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], payload.job_id, payload.status, payload.notes, payload.application_url, payload.contact_email, payload.email_subject, payload.email_body, now, now),
        )
        db.commit()
        row = db.execute(
            "SELECT a.*, j.title, j.company, j.location, j.url FROM applications a LEFT JOIN jobs j ON j.id = a.job_id WHERE a.id = ? AND a.user_id = ?",
            (cursor.lastrowid, user["id"]),
        ).fetchone()
    return {"application": dict(row)}


@app.put("/api/v1/applications/{application_id}")
def update_application(
    application_id: int,
    payload: ApplicationUpdate,
    user: sqlite3.Row = Depends(user_dependency),
) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    allowed = {"status", "notes", "application_url", "contact_email", "email_subject", "email_body"}
    updates = {key: value for key, value in updates.items() if key in allowed}
    with db_lock:
        existing = db.execute(
            "SELECT id FROM applications WHERE id = ? AND user_id = ?",
            (application_id, user["id"]),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Application not found")
        if updates:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            db.execute(
                f"UPDATE applications SET {assignments}, updated_at = ? WHERE id = ? AND user_id = ?",
                [*updates.values(), utc_now(), application_id, user["id"]],
            )
            db.commit()
        row = db.execute(
            "SELECT a.*, j.title, j.company, j.location, j.url FROM applications a LEFT JOIN jobs j ON j.id = a.job_id WHERE a.id = ? AND a.user_id = ?",
            (application_id, user["id"]),
        ).fetchone()
    return {"application": dict(row)}

