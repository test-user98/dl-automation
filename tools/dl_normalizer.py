"""
DL number normalizer — accepts any format the customer types and
returns the canonical form the Sarathi portal expects.

Indian DL format: {STATE_CODE}{RTO_CODE}{YEAR}{SEQUENCE}
  Example: RJ0720170010191
    RJ   = Rajasthan state code
    07   = RTO district code
    2017 = year of issue
    0010191 = sequence number (7 digits)

Users type it in many ways:
  "RJ07 2017 0010191"    → RJ0720170010191
  "RJ-07-2017-0010191"   → RJ0720170010191
  "rj0720170010191"      → RJ0720170010191
  "RJ/07/2017/0010191"   → RJ0720170010191
"""

import re

STATE_CODES = {
    "AN": "Andaman and Nicobar",
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CH": "Chandigarh",
    "CG": "Chhattisgarh",
    "DD": "Daman and Diu",
    "DL": "Delhi",
    "DN": "Dadra and Nagar Haveli",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HR": "Haryana",
    "HP": "Himachal Pradesh",
    "JK": "Jammu and Kashmir",
    "JH": "Jharkhand",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MP": "Madhya Pradesh",
    "MH": "Maharashtra",
    "MN": "Manipur",
    "ML": "Meghalaya",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OD": "Odisha",
    "PY": "Pondicherry",
    "PB": "Punjab",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TS": "Telangana",
    "TR": "Tripura",
    "UP": "Uttar Pradesh",
    "UK": "Uttarakhand",
    "WB": "West Bengal",
}

# Format hint shown to user in the app
DL_FORMAT_HINT = (
    "Your DL number is printed on the front of your driving license. "
    "It starts with your state code (e.g. RJ for Rajasthan, DL for Delhi, MH for Maharashtra) "
    "followed by numbers. Example: RJ0720170010191 or DL-04-2011-0012345"
)

# Where to find it on the card
DL_LOCATION_HINT = "Look for the number below your name on the front of the card, usually labeled 'Licence No.' or 'DL No.'"


class DLNormalizer:

    def normalize(self, raw: str) -> dict:
        """
        Normalize a DL number from any user input format.
        Returns:
          {
            "normalized": "RJ0720170010191",  # canonical form
            "state_code": "RJ",
            "state_name": "Rajasthan",
            "rto_code":   "RJ07",
            "year":       "2017",
            "valid":      True,
            "error":      ""
          }
        """
        if not raw:
            return self._error("Please enter your DL number")

        # Strip all separators and uppercase
        cleaned = re.sub(r"[\s\-/\\.]", "", raw.strip().upper())

        # Must start with 2 alpha chars (state code)
        if not re.match(r"^[A-Z]{2}", cleaned):
            return self._error(
                f"DL number should start with your state code (e.g. RJ, DL, MH). "
                f"You entered: {raw}"
            )

        state_code = cleaned[:2]
        if state_code not in STATE_CODES:
            return self._error(
                f"'{state_code}' is not a recognised state code. "
                f"Valid examples: RJ, DL, MH, KA, UP"
            )

        # Rest must be digits
        rest = cleaned[2:]
        if not rest.isdigit():
            return self._error(
                "After the state code, the DL number should contain only digits. "
                f"Got: {rest}"
            )

        # Total length should be 13-15 chars
        if len(cleaned) < 13 or len(cleaned) > 16:
            return self._error(
                f"DL number looks too {'short' if len(cleaned) < 13 else 'long'}. "
                f"Expected 13-15 characters total, got {len(cleaned)}. "
                f"Please double-check: {raw}"
            )

        # Extract components (best-effort — formats vary by state)
        rto_code   = cleaned[:4]    # e.g. RJ07
        year       = cleaned[4:8] if len(cleaned) >= 8 else ""
        state_name = STATE_CODES.get(state_code, "")

        return {
            "normalized": cleaned,
            "state_code": state_code,
            "state_name": state_name,
            "rto_code":   rto_code,
            "year":       year,
            "valid":      True,
            "error":      "",
        }

    def format_for_display(self, normalized: str) -> str:
        """Format for human-readable display: RJ07 2017 0010191"""
        if len(normalized) >= 8:
            return f"{normalized[:4]} {normalized[4:8]} {normalized[8:]}"
        return normalized

    @staticmethod
    def _error(msg: str) -> dict:
        return {
            "normalized": "",
            "state_code": "",
            "state_name": "",
            "rto_code":   "",
            "year":       "",
            "valid":      False,
            "error":      msg,
        }
