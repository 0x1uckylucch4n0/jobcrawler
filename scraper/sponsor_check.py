"""
Downloads and checks the UK Home Office register of licensed Skilled Worker visa sponsors.
Updated monthly by the government.
"""
import requests
import pandas as pd
import re
import os
import time

CACHE_FILE = "sponsor_register.csv"
CACHE_MAX_AGE_DAYS = 7
GOV_PAGE_URL = "https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers"

# Legal suffixes stripped from register names to improve fuzzy matching
_LEGAL_SUFFIXES = (
    " plc", " ltd", " limited", " llp", " llc", " inc", " corp", " corporation",
    " international", " group", " holdings", " uk", " europe", " securities",
    " partners", " services", " solutions", " & co", " and co", " (uk)",
    " global", " capital", " management", " advisory",
)


def _normalise(name: str) -> str:
    """Strip legal suffixes iteratively to get a bare company name."""
    changed = True
    while changed:
        changed = False
        for s in _LEGAL_SUFFIXES:
            if name.endswith(s):
                name = name[:-len(s)].strip()
                changed = True
    return name


def _download_register() -> pd.DataFrame:
    """Fetch the latest sponsor register CSV from gov.uk."""
    print("Downloading UK sponsor register...")
    resp = requests.get(GOV_PAGE_URL, timeout=30)
    resp.raise_for_status()

    # Find the CSV download link on the page
    matches = re.findall(r'https://assets\.publishing\.service\.gov\.uk[^"\']+\.csv', resp.text)
    if not matches:
        raise RuntimeError("Could not find sponsor register CSV link on gov.uk")

    csv_url = matches[0]
    print(f"Found register at: {csv_url}")

    csv_resp = requests.get(csv_url, timeout=60)
    csv_resp.raise_for_status()

    with open(CACHE_FILE, "wb") as f:
        f.write(csv_resp.content)

    print(f"Sponsor register saved ({len(csv_resp.content) // 1024}KB)")
    return pd.read_csv(CACHE_FILE)


def load_register() -> set:
    """Return a set of company names that are licensed sponsors.

    Includes both full register names and suffix-stripped variants so that
    'Mastercard' matches 'Mastercard International', 'BAE Systems' matches
    'BAE Systems plc', etc.
    """
    # Use cache if fresh enough
    if os.path.exists(CACHE_FILE):
        age_days = (time.time() - os.path.getmtime(CACHE_FILE)) / 86400
        if age_days < CACHE_MAX_AGE_DAYS:
            print("Using cached sponsor register.")
            df = pd.read_csv(CACHE_FILE)
        else:
            df = _download_register()
    else:
        df = _download_register()

    # Normalise company names to lowercase for matching
    name_col = df.columns[0]  # First column is always organisation name
    raw_names = df[name_col].dropna().str.lower().str.strip().tolist()

    sponsors = set(raw_names)
    # Add suffix-stripped variants for fuzzy matching
    for name in raw_names:
        stripped = _normalise(name)
        if stripped and stripped != name:
            sponsors.add(stripped)

    print(f"Loaded {len(sponsors):,} sponsor entries (with normalised variants).")
    return sponsors


def is_sponsor(company_name: str, sponsor_set: set) -> bool:
    """Check if a company is a licensed Skilled Worker visa sponsor."""
    if not company_name:
        return False
    name = company_name.lower().strip()
    if name in sponsor_set:
        return True
    # Also try stripping suffixes from the queried name
    normalised = _normalise(name)
    if normalised and normalised != name and normalised in sponsor_set:
        return True
    return False
