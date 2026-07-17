"""
Static mapping of congressional committees (by their stable `thomas_id`) to the
industries/sectors they regulate or oversee. This lets the app flag stocks that
may be relevant to a politician's committee assignments (a common signal used
to spot potential conflicts of interest).

There's no authoritative government API for "committee -> industry", so this
mapping was curated by hand from each committee's official jurisdiction
description. It's intentionally editable -- feel free to tweak the tags.
"""

COMMITTEE_SECTORS = {
    # ---- House committees ----
    "HSAG": ["Agriculture", "Food & Beverage", "Commodities", "Biotechnology"],
    "HSAP": ["Government Contractors", "Defense", "Infrastructure"],
    "HSAS": ["Defense", "Aerospace", "Military Contractors"],
    "HSBA": ["Banking", "Financial Services", "Insurance", "Real Estate", "Fintech", "Cryptocurrency"],
    "HSBU": ["Fiscal Policy", "Government Spending"],
    "HSED": ["Education", "Labor", "Pensions"],
    "HSFA": ["Defense", "Foreign Trade", "International"],
    "HSGO": ["Government Services", "Regulatory Compliance"],
    "HSHA": ["Media", "Elections Technology"],
    "HSHM": ["Homeland Security", "Cybersecurity", "Aerospace", "Defense"],
    "HSIF": ["Energy", "Telecommunications", "Healthcare", "Pharmaceuticals", "Consumer Products", "Technology"],
    "HSII": ["Energy", "Mining", "Oil & Gas", "Utilities", "Agriculture"],
    "HSJU": ["Technology", "Antitrust", "Pharmaceuticals", "Media"],
    "HSPW": ["Transportation", "Airlines", "Railroads", "Shipping", "Infrastructure", "Construction"],
    "HSRU": ["Government Procedure"],
    "HSSM": ["Small Business", "Banking"],
    "HSSO": ["Ethics"],
    "HSSY": ["Aerospace", "Technology", "Energy", "Space"],
    "HSVR": ["Healthcare", "Pharmaceuticals", "Government Services"],
    "HSWM": ["Taxation", "Healthcare", "Social Security", "Trade", "Retirement"],
    "HLIG": ["Defense", "Technology", "Cybersecurity", "Aerospace"],
    "HSZS": ["Technology", "Semiconductors", "Defense", "International Trade"],

    # ---- Senate committees ----
    "SSAF": ["Agriculture", "Food & Beverage", "Commodities", "Biotechnology"],
    "SSAP": ["Government Contractors", "Defense", "Infrastructure"],
    "SSAS": ["Defense", "Aerospace", "Military Contractors"],
    "SSBK": ["Banking", "Financial Services", "Insurance", "Real Estate", "Fintech", "Cryptocurrency", "Housing"],
    "SSBU": ["Fiscal Policy", "Government Spending"],
    "SSCM": ["Telecommunications", "Media", "Transportation", "Technology", "Aerospace", "Space"],
    "SSEG": ["Energy", "Oil & Gas", "Utilities", "Mining", "Nuclear"],
    "SSEV": ["Environment", "Infrastructure", "Nuclear", "Chemicals", "Utilities"],
    "SSFI": ["Taxation", "Healthcare", "Trade", "Social Security", "Retirement"],
    "SSFR": ["Defense", "Foreign Trade", "International"],
    "SSGA": ["Government Services", "Cybersecurity", "Regulatory Compliance"],
    "SSHR": ["Healthcare", "Pharmaceuticals", "Education", "Labor", "Biotechnology"],
    "SSJU": ["Technology", "Antitrust", "Pharmaceuticals", "Media", "Cryptocurrency"],
    "SSRA": ["Government Procedure", "Media"],
    "SSSB": ["Small Business", "Banking"],
    "SSVA": ["Healthcare", "Pharmaceuticals", "Government Services"],
    "SLIA": ["Gaming & Casinos", "Energy", "Mining"],
    "SLIN": ["Defense", "Technology", "Cybersecurity", "Aerospace"],
    "SLET": ["Ethics"],
    "SPAG": ["Healthcare", "Pharmaceuticals", "Retirement", "Insurance"],
    "SCNC": ["Pharmaceuticals", "Healthcare"],

    # ---- Joint committees ----
    "JSEC": ["Fiscal Policy", "Economic Policy"],
    "JSTX": ["Taxation"],
    "JSLC": ["Government Services"],
    "JSPR": ["Media", "Publishing"],
}


def get_sectors_for_thomas_id(thomas_id):
    """Return a list of sector tags for a committee thomas_id, or [] if unknown."""
    return COMMITTEE_SECTORS.get(thomas_id, [])
