"""Static US state/territory name -> two-letter code mapping.

Used only to normalize the Congress.gov API's full state names (e.g.
"California") into the same two-letter codes (e.g. "CA") already used
throughout the app and stored by the default `congress-legislators`
YAML-based pipeline, so the two data sources are interchangeable in the UI.
"""

STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    # Territories / DC (non-voting delegates still file trade disclosures)
    "District of Columbia": "DC", "Puerto Rico": "PR", "Guam": "GU",
    "American Samoa": "AS", "United States Virgin Islands": "VI",
    "Virgin Islands": "VI", "Northern Mariana Islands": "MP",
}


def state_name_to_code(name):
    if not name:
        return ""
    return STATE_NAME_TO_CODE.get(name.strip(), name.strip()[:2].upper())
