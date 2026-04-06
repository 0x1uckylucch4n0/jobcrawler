"""
Searches jobs across LinkedIn and Indeed for all target locations.
Filters: full-time, analyst/associate level, correct sector, visa sponsorship.
"""
import requests
from jobspy import scrape_jobs
from cv_profile import (
    SEARCH_TERMS, EXCLUDE_TERMS, EXCLUDE_TITLE_TERMS,
    CV_PROFILE, TARGET_LOCATIONS, TARGET_COMPANIES, TARGET_SECTORS
)
from sponsor_check import load_register, is_sponsor
from job_store import is_new, mark_seen
from summarizer import summarise_job

_STATUS_CACHE: dict[str, tuple[bool, bool]] = {}  # url -> (closed, easy_apply)

_LI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


_DEAD_PHRASES = (
    "no longer accepting applications",
    "job is no longer available",
    "this job has expired",
    "position has been filled",
    "vacancy has been filled",
    "posting has been removed",
    "this position is no longer",
    "job posting is no longer",
)


def _linkedin_job_status(job_url: str) -> tuple[bool, bool]:
    """Fetch LinkedIn job page once and return (is_closed, is_easy_apply)."""
    if "linkedin.com" not in job_url:
        return False, False
    if job_url in _STATUS_CACHE:
        return _STATUS_CACHE[job_url]
    try:
        resp = requests.get(job_url, headers=_LI_HEADERS, timeout=10, allow_redirects=True)
        if "authwall" in resp.url or "login" in resp.url or resp.status_code in (401, 403):
            _STATUS_CACHE[job_url] = (False, False)
            return False, False
        if resp.status_code == 404:
            _STATUS_CACHE[job_url] = (True, False)
            return True, False
        text = resp.text.lower()
        closed = any(p in text for p in _DEAD_PHRASES)
        easy_apply = "easy apply" in text
        _STATUS_CACHE[job_url] = (closed, easy_apply)
        return closed, easy_apply
    except Exception:
        return False, False


def _direct_url_is_live(url: str) -> bool:
    """Check if a direct company URL is still live (not 404 or expired)."""
    if not url or "linkedin.com" in url:
        return True
    try:
        resp = requests.get(url, headers=_LI_HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code == 404 or resp.status_code >= 400:
            return False
        text = resp.text.lower()
        return not any(p in text for p in _DEAD_PHRASES)
    except Exception:
        return True  # on error assume live


_EXCLUDED_ROLE_KEYWORDS = [
    # Law
    'lawyer', 'solicitor', 'paralegal', 'barrister', 'legal counsel', 'legal advisor',
    'legal assistant', 'law clerk', 'attorney', 'conveyancer',
    # Customer support / service
    'customer support', 'customer service', 'customer success', 'client support',
    'help desk', 'helpdesk', 'service desk', 'support specialist', 'support agent',
    # Admin
    'administrative', 'receptionist', 'office manager', 'secretary',
    'personal assistant', 'executive assistant', 'office coordinator',
    # Software engineering
    'software engineer', 'software developer', 'backend engineer', 'frontend engineer',
    'full stack', 'fullstack', 'devops', 'sre ', 'site reliability',
    'ios developer', 'android developer', 'mobile developer', 'web developer',
    'qa engineer', 'test engineer', 'quality assurance engineer',
    'data engineer', 'machine learning engineer', 'ml engineer', 'ai engineer',
    'cloud engineer', 'infrastructure engineer', 'platform engineer',
    'security engineer', 'cybersecurity engineer', 'network engineer',
    # Other specialized
    'nurse', 'doctor', 'teacher', 'lecturer', 'chef', 'driver', 'warehouse',
    'mechanical engineer', 'civil engineer', 'electrical engineer',
    'accountant', 'bookkeeper', 'payroll',
    'recruiter', 'talent acquisition',
    'marketing', 'social media', 'content',
    'designer', 'ux ', 'ui ',
    'sales ', 'sales,', 'account executive', 'business development representative',
]


def _is_excluded_role(title: str) -> bool:
    t = f" {title.lower()} "
    return any(kw in t for kw in _EXCLUDED_ROLE_KEYWORDS)


def _is_grad_scheme(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    return any(term in text for term in EXCLUDE_TERMS)


def _is_too_senior(title: str) -> bool:
    t = f" {title.lower()} "
    return any(term in t for term in EXCLUDE_TITLE_TERMS)


def _explicitly_no_sponsorship(description: str) -> bool:
    """Returns True if the job description explicitly rules out visa sponsorship."""
    text = description.lower()
    no_sponsorship_phrases = [
        # LinkedIn structured requirement fields
        "no need for visa sponsorship",
        "authorized to work in united kingdom",
        "authorised to work in united kingdom",
        "authorized to work in the united kingdom",
        "authorised to work in the united kingdom",
        # General no-sponsorship statements
        "no visa sponsorship",
        "unable to offer visa sponsorship",
        "cannot offer visa sponsorship",
        "does not offer visa sponsorship",
        "will not provide visa sponsorship",
        "sponsorship is not available",
        "visa sponsorship is not provided",
        "visa sponsorship will not be provided",
        "sponsorship is not provided",
        "not able to offer sponsorship",
        "not in a position to offer sponsorship",
        "we do not sponsor",
        "we are unable to sponsor",
        "we cannot sponsor",
        "sponsorship cannot be provided",
        "unfortunately we are unable to sponsor",
        # Right to work variants
        "right to work in the uk",
        "right to work in uk",
        "right to work in the united kingdom",
        "right to work in united kingdom",
        "must have the right to work",
        "must already have the right to work",
        "must be eligible to work in the uk",
        "must be authorised to work in the uk",
        "must be authorized to work in the uk",
        "authorised to work in the uk",
        "authorized to work in the uk",
        "unrestricted right to work",
        "candidates must have permission to work",
        "eligible to work in the uk without sponsorship",
        "eligible to work without sponsorship",
    ]
    return any(phrase in text for phrase in no_sponsorship_phrases)


def _is_wrong_sector(title: str, description: str) -> bool:
    """Reject clearly irrelevant sectors (NHS, public sector, etc.)"""
    text = f"{title} {description}".lower()
    return any(term in text for term in EXCLUDE_TERMS)


def _is_target_company(company: str) -> bool:
    c = company.lower()
    return any(t.lower() in c or c in t.lower() for t in TARGET_COMPANIES)


def _score_job(title: str, company: str, description: str) -> int:
    text = f"{title} {description}".lower()
    skill_matches = sum(1 for skill in CV_PROFILE["skills"] if skill.lower() in text)
    sector_matches = sum(1 for sector in TARGET_SECTORS if sector in text)
    company_bonus = 3 if _is_target_company(company) else 0
    return min(10, skill_matches + sector_matches + company_bonus)


def _valid_amount(v: str) -> bool:
    return bool(v) and v not in ("nan", "None", "none", "")


def search_jobs(max_results: int = 10, new_only: bool = False) -> list[dict]:
    sponsors = load_register()
    all_jobs = []
    seen_keys = set()

    # London first, then secondary locations
    locations = sorted(TARGET_LOCATIONS, key=lambda x: (not x["primary"]))

    for loc in locations:
        for term in SEARCH_TERMS:
            print(f"Searching '{term}' in {loc['label']}...")
            try:
                jobs = scrape_jobs(
                    site_name=["linkedin", "google"],
                    search_term=term,
                    location=loc["jobspy_location"],
                    country_indeed=loc.get("country", "UK"),
                    results_wanted=50,
                    hours_old=720,
                    linkedin_fetch_description=False,
                )
            except Exception as e:
                if "Invalid country string" in str(e):
                    # jobspy sometimes returns results from neighboring countries
                    # (e.g. Liechtenstein for Swiss searches) — skip silently
                    continue
                print(f"  Error: {e}")
                continue

            if jobs is None or jobs.empty:
                continue

            for _, row in jobs.iterrows():
                title = str(row.get("title", "")).strip()
                company = str(row.get("company", "")).strip()

                if not title or not company:
                    continue

                dedup_key = f"{title.lower()}|{company.lower()}"
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                if new_only and not is_new(title, company):
                    continue

                description = str(row.get("description", ""))
                location_str = str(row.get("location", loc["label"]))
                job_type = str(row.get("job_type", "")).lower()

                job_url = str(row.get("job_url", ""))
                job_url_direct = str(row.get("job_url_direct", ""))
                is_linkedin = "linkedin.com" in job_url

                AGGREGATORS = ("indeed.com", "glassdoor.", "reed.co", "totaljobs.", "cwjobs.",
                               "monster.", "jobsite.", "ziprecruiter.", "google.com/search")
                direct_is_clean = (
                    job_url_direct not in ("", "nan", "None", "none")
                    and not any(a in job_url_direct for a in AGGREGATORS)
                )

                # Only keep jobs from LinkedIn or a direct company website
                if not is_linkedin and not direct_is_clean:
                    continue

                url = job_url if is_linkedin else job_url_direct

                # Full-time only
                if job_type and "full" not in job_type and job_type not in ("nan", "none", "fulltime", ""):
                    continue

                # No grad schemes / internships / wrong sectors
                if _is_grad_scheme(title, description):
                    continue

                # No senior titles
                if _is_too_senior(title):
                    continue

                # Skip excluded role types (law, admin, software eng, etc.)
                if _is_excluded_role(title):
                    print(f"  Skipped (excluded role): {title} @ {company}")
                    continue

                # Skip if description explicitly says no visa sponsorship
                if _explicitly_no_sponsorship(description):
                    print(f"  Skipped (no sponsorship stated): {title} @ {company}")
                    continue

                sponsor_confirmed = is_sponsor(company, sponsors) if loc["label"] == "London" else None
                score = _score_job(title, company, description)

                # Skip very low scoring jobs unless from a target company
                if score < 2 and not _is_target_company(company):
                    continue

                # Check job is still live before including
                is_easy_apply = False
                if is_linkedin:
                    closed, is_easy_apply = _linkedin_job_status(url)
                    if closed:
                        print(f"  Skipped (closed): {title} @ {company}")
                        continue
                elif not _direct_url_is_live(url):
                    print(f"  Skipped (dead link): {title} @ {company}")
                    continue

                # AI-generated summary
                summary = summarise_job(title, company, description)

                job = {
                    "title": title,
                    "company": company,
                    "location": location_str,
                    "search_location": loc["label"],
                    "url": url,
                    "score": score,
                    "is_sponsor": sponsor_confirmed,
                    "summary": summary,
                    "is_target_company": _is_target_company(company),
                    "is_easy_apply": is_easy_apply,
                }
                all_jobs.append(job)

                if new_only:
                    mark_seen(title, company, location_str, url, description)

    # Sort: target companies first, confirmed sponsors, then score
    all_jobs.sort(key=lambda x: (
        not x["is_target_company"],
        x["is_sponsor"] is False,
        x["is_sponsor"] is None,
        -x["score"]
    ))

    return all_jobs[:max_results]
