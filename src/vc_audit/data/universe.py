"""The candidate universe: which listed companies to consider, by sector.

A curated per-sector ticker list is the deterministic baseline for peer
selection. It is transparent (the whole universe is visible in this file and in
the audit trail), reproducible (the same sector yields the same candidates every
run), and free of model risk.

Its limitation is that it is static, and a static list is a judgement someone
made once. The optional research layer (:mod:`vc_audit.research`) addresses that
by proposing candidates per company, and its proposals are merged with -- never
silently substituted for -- this baseline, so a reviewer can always see which
candidates a model introduced.

Membership here is a claim about *sector*, not about comparability. Fundamentals
come from each company's own SEC filings; nothing about their financials is
asserted in this file.
"""

from __future__ import annotations

#: Liquid, US-listed, current filers with clean XBRL. Chosen for coverage of the
#: sector's business models rather than for size.
SECTOR_UNIVERSE: dict[str, tuple[str, ...]] = {
    "saas": (
        "CRM",   # Salesforce
        "NOW",   # ServiceNow
        "WDAY",  # Workday
        "DDOG",  # Datadog
        "SNOW",  # Snowflake
        "HUBS",  # HubSpot
        "TEAM",  # Atlassian
        "ZS",    # Zscaler
        "MDB",   # MongoDB
        "NET",   # Cloudflare
        "TWLO",  # Twilio
        "OKTA",  # Okta
        "INTA",  # Intapp, vertical SaaS for legal and professional services
        "GTLB",  # GitLab
        "BILL",  # BILL Holdings
        "BRZE",  # Braze
        "PCOR",  # Procore
        "ASAN",  # Asana
        "DOCU",  # DocuSign
        "WK",    # Workiva
        "BOX",   # Box
    ),
    "fintech": (
        "PYPL",  # PayPal
        "NCNO",  # nCino, banking SaaS
        "GPN",   # Global Payments
        "TOST",  # Toast
        "AFRM",  # Affirm
        "SOFI",  # SoFi
        "UPST",  # Upstart
        "MQ",    # Marqeta
        "FOUR",  # Shift4
        "BILL",  # BILL Holdings
    ),
    "marketplace": (
        "ABNB",  # Airbnb
        "UBER",  # Uber
        "DASH",  # DoorDash
        "ETSY",  # Etsy
        "EBAY",  # eBay
        "LYFT",  # Lyft
        "CHWY",  # Chewy
        "W",     # Wayfair
        "FVRR",  # Fiverr
        "UPWK",  # Upwork
    ),
    "healthtech": (
        "VEEV",  # Veeva Systems
        "DOCS",  # Doximity
        "TDOC",  # Teladoc
        "HIMS",  # Hims & Hers
        "OSCR",  # Oscar Health
        "ALHC",  # Alignment Healthcare
        "PGNY",  # Progyny
        "GDRX",  # GoodRx
    ),
}


def candidates_for(sector: str) -> tuple[str, ...]:
    """Tickers to consider for ``sector``. Empty tuple when unmapped."""
    return SECTOR_UNIVERSE.get(sector.strip().lower(), ())


def known_sectors() -> list[str]:
    return sorted(SECTOR_UNIVERSE)
