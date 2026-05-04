"""
Google Sheets configuration constants (CR-4).

Maps column positions and defines condition grade-to-numeric conversions
for the GreenBay evaluation tracking spreadsheet.
"""

# The Google Sheet ID (from the URL)
SHEET_ID = "1DCKTWSxvGYQzuEPoJanQFF5ssM9oWnqVmEoEmh1MhAU"

# Tab name in the Google Sheet
TAB_NAME = "Customer Initiated Evaluation"

# Column mapping (0-indexed for gspread)
# A=0, B=1, ..., M=12
COL_DATE = 0               # A: Date
COL_ITEM = 1               # B: Item (e.g. "Samsung 350L Fridge")
COL_CONDITION = 2          # C: Condition (1-5)
COL_AGE = 3                # D: Age (e.g. "2 years")
COL_NEW_PRICE = 4          # E: New price (estimate)
COL_TRADE_IN = 5           # F: Customer wants to trade in?
COL_AI_PRICE = 6           # G: AI Price
COL_AI_CONFIDENCE = 7      # H: AI Confidence Score
COL_CUSTOMER_PRICE = 8     # I: Customer Selling Price
COL_INTERNAL_PRICE = 9     # J: Internal Team Price — DO NOT WRITE
COL_FINAL_PRICE = 10       # K: Final Price Offered — DO NOT WRITE
COL_STATUS = 11            # L: Accepted / Rejected
COL_NOTES = 12             # M: Notes

# Columns we are ALLOWED to write to (never J or K)
WRITABLE_COLUMNS = [
    COL_DATE, COL_ITEM, COL_CONDITION, COL_AGE, COL_NEW_PRICE,
    COL_TRADE_IN, COL_AI_PRICE, COL_AI_CONFIDENCE, COL_CUSTOMER_PRICE,
    COL_STATUS, COL_NOTES,
]

# ---------------------------------------------------------------------------
# Condition grade → numeric (1-5) for Google Sheet column C
# ---------------------------------------------------------------------------
# The site uses descriptive conditions mapped to letter grades A/B/C/D.
# The sheet uses a 1-5 scale. This bridges the two.
CONDITION_TO_NUMERIC = {
    # From site condition values
    "new": 5,
    "like_new": 5,
    "open_box": 4,
    "slightly_used": 4,
    "used": 3,
    "working_issues": 2,
    "partially_working": 1,
    "not_working": 1,
    # From letter grades
    "A": 5,
    "B": 4,
    "C": 3,
    "D": 2,
}

# Age display strings for the sheet
AGE_DISPLAY = {
    0: "Brand New",
    0.5: "Under 1 year",
    1: "1 year",
    1.5: "1-2 years",
    2: "2 years",
    2.5: "2-3 years",
    3: "3 years",
    4: "3-5 years",
    5: "5 years",
    6: "5-8 years",
    7: "5-8 years",
    8: "8+ years",
}


def age_to_display(age_years: float) -> str:
    """Convert numeric age to display string for the sheet."""
    if age_years in AGE_DISPLAY:
        return AGE_DISPLAY[age_years]
    if age_years < 1:
        return "Under 1 year"
    if age_years <= 2:
        return f"{int(age_years)} year{'s' if age_years > 1 else ''}"
    if age_years <= 5:
        return f"{int(age_years)} years"
    if age_years <= 8:
        return "5-8 years"
    return "8+ years"


def format_price_range(low: float, high: float) -> str:
    """Format a price range in the 'Xk to Yk' style matching existing sheet data."""
    def _fmt(val: float) -> str:
        if val >= 1000:
            k = val / 1000
            if k == int(k):
                return f"{int(k)}k"
            return f"{k:.1f}k"
        return str(int(val))

    if low == high:
        return _fmt(low)
    return f"{_fmt(low)} to {_fmt(high)}"
