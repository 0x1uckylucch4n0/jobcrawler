CV_PROFILE = {
    "name": "Alramina Myrzabekova",
    "experience_years": 2,
    "current_role": "Technology Risk Consultant, EY Financial Services",
    "skills": [
        "technology risk", "IT risk", "IT governance", "IT consulting",
        "risk assessment", "process redesign", "stakeholder management",
        "requirements gathering", "product management", "project management",
        "RFP", "digital transformation", "platform implementation",
        "data migration", "compliance", "SQL", "Excel", "PowerBI", "Python",
        "data analysis", "data warehouse", "due diligence", "financial services",
        "fintech", "banking", "asset management", "venture capital",
        "strategy", "consulting", "investment banking", "private equity",
        "M&A", "growth equity", "programme advisory", "business analyst",
        "AI advisory", "technology transformation",
    ],
}

# Search terms — analyst/associate level only, based on actual application history
SEARCH_TERMS = [
    # Investment banking
    "investment banking analyst",
    "M&A analyst",
    "M&A associate",
    "deal analyst",
    "capital markets analyst",
    "leveraged finance analyst",
    "DCF analyst",
    "corporate finance analyst",
    # Private equity / VC / funds
    "private equity analyst",
    "private equity associate",
    "venture capital analyst",
    "venture capital associate",
    "growth equity analyst",
    "fund analyst",
    "hedge fund analyst",
    "asset management analyst",
    "equity research analyst",
    "investment analyst",
    "portfolio analyst",
    "credit analyst",
    # Consulting
    "strategy consultant analyst",
    "management consultant analyst",
    "strategy analyst",
    "management consulting analyst",
    "business consultant analyst",
    "technology consultant analyst",
    "transformation analyst",
    "advisory analyst",
    # Tech / risk / data
    "technology risk analyst",
    "IT risk analyst",
    "cyber risk analyst",
    "risk analyst fintech",
    "risk analyst banking",
    "quantitative analyst",
    "data analyst financial services",
    "data analyst banking",
    "business analyst fintech",
    "business analyst financial services",
    "product analyst fintech",
    "product analyst banking",
    # Fintech / operations / compliance
    "fintech analyst",
    "payments analyst",
    "compliance analyst financial services",
    "regulatory analyst",
    "operations analyst fintech",
    "operations analyst banking",
    "financial analyst",
    "commercial analyst",
    "FP&A analyst",
]

# Title keywords that indicate too senior for 2 years experience
EXCLUDE_TITLE_TERMS = [
    "senior", "director", "head of", "vice president", " vp ",
    "executive", "chief", "principal", "managing director", " md ",
    "partner", " manager", " lead ", "svp", "evp", "president",
    "officer", "c-suite",
]

# Exclude these from title OR description — grad schemes, internships, wrong sector
EXCLUDE_TERMS = [
    # Wrong seniority
    "graduate scheme", "graduate programme", "grad scheme",
    "internship", "placement year", "sandwich year",
    "apprenticeship", "traineeship",
    # Wrong sector — explicitly excluded
    "nhs", "national health service", "civil service",
    "local authority", "local council", "city council",
    "public sector", "government department", "ministry of",
    "charity", "non-profit", "non profit", "third sector",
    "housing association", "social care", "social worker",
]

# Target companies — prioritised in results
TARGET_COMPANIES = {
    # Already applied to
    "McKinsey", "Bain", "BCG", "OC&C", "Oliver Wyman", "Fairgrove Partners", "Konrad",
    "EY", "Accenture", "BAE Systems", "AON",
    "Goldman Sachs", "Morgan Stanley", "JPMorgan", "Deutsche Bank", "PIMCO",
    "PJT Partners", "Stifel Europe", "Guggenheim Securities",
    "BlackRock", "Schroders", "GLG Insights",
    "Revolut", "Visa", "Uber", "Liberis",
    # New targets — Strategy & Management Consulting
    "Roland Berger", "Kearney", "LEK Consulting", "Arthur D. Little",
    "Strategy&", "Deloitte", "Monitor Deloitte", "Alvarez & Marsal",
    "FTI Consulting", "Efficio", "Analysys Mason", "Teneo",
    "PA Consulting", "Sia Partners",
    # Tech Strategy / AI Consulting
    "Palantir", "Quantexa", "Behavox", "Faculty AI", "Wayve",
    "Onfido", "Eigen Technologies", "Cleo", "Thought Machine",
    # Fintech / Scale-ups
    "Monzo", "Starling Bank", "Checkout.com", "SumUp", "GoCardless",
    "Paysafe", "iwoca", "Funding Circle", "Tide", "Zilch",
    # Venture Capital / Growth Equity
    "Atomico", "Balderton Capital", "Index Ventures", "Accel",
    "Sequoia", "General Catalyst", "Highland Europe", "Northzone",
    "Dawn Capital", "LocalGlobe", "Notion Capital", "Episode 1",
    # Private Equity
    "Permira", "Apax Partners", "CVC Capital", "Advent International",
    "Bridgepoint", "HgCapital", "Vitruvian Partners", "Francisco Partners",
    # Investment Banking (boutiques + mid-market)
    "Lazard", "Jefferies", "Rothschild", "Evercore", "Moelis",
    "Houlihan Lokey", "Numis", "Greenhill",
    # Expert Networks
    "Gartner", "Forrester", "Third Bridge", "Guidepoint",
    "Coleman Research", "Tegus",
}

# Target sectors — used for relevance scoring
TARGET_SECTORS = [
    "financial services", "fintech", "technology", "private equity",
    "venture capital", "asset management", "investment banking",
    "management consulting", "strategy consulting", "AI", "data",
]

# Locations
TARGET_LOCATIONS = [
    {"label": "London", "jobspy_location": "London, UK", "country": "UK", "primary": True},
    {"label": "San Francisco", "jobspy_location": "San Francisco, CA", "country": "US", "primary": False},
    {"label": "Palo Alto", "jobspy_location": "Palo Alto, CA", "country": "US", "primary": False},
    {"label": "Paris", "jobspy_location": "Paris, France", "country": "FR", "primary": False},
    {"label": "Zurich", "jobspy_location": "Zurich, Switzerland", "country": "CH", "primary": False},
    {"label": "Geneva", "jobspy_location": "Geneva, Switzerland", "country": "CH", "primary": False},
]
