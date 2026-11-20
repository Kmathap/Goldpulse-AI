"""Built-in backup calendar: official release dates of the US events that move gold most.

Used only when the live ForexFactory feed cannot be reached (it often refuses
shared cloud servers such as Render's free plan). Dates are copied from the
official publishers; all times are US Eastern (New York) time.

Sources (checked 20 Nov 2026):
* BLS release calendar  – bls.gov/schedule (CPI, Employment Situation, PPI)
* BEA release schedule  – bea.gov/news/schedule (Personal Income and Outlays = PCE inflation)
* Federal Reserve       – federalreserve.gov FOMC calendar, incl. the revised 2027
                          schedule of 12 Nov 2026 (policy decision 2:00 pm ET on day 2;
                          the 26–27 Oct 2027 meeting has no policy decision)

Update this list about once a year, or when an agency announces a change.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
SOURCE = "Official US release schedule (built-in: BLS, BEA, Federal Reserve)"
LAST_DATE = "2027-11-17"     # after this the list is out of date

# (date, time ET, title, impact)
EVENTS: list[tuple[str, str, str, str]] = [
    # --- BEA: Personal Income and Outlays (PCE price index)
    ("2026-11-25", "08:30", "Core PCE Price Index / Personal Income & Outlays (Oct)", "High"),
    ("2027-01-28", "08:30", "Core PCE Price Index / Personal Income & Outlays (Dec)", "High"),
    # --- BLS: Employment Situation (Non-Farm Payrolls)
    ("2026-12-04", "08:30", "Non-Farm Employment Change / Unemployment Rate (Nov)", "High"),
    ("2027-01-08", "08:30", "Non-Farm Employment Change / Unemployment Rate (Dec)", "High"),
    ("2027-02-05", "08:30", "Non-Farm Employment Change / Unemployment Rate (Jan)", "High"),
    ("2027-04-02", "08:30", "Non-Farm Employment Change / Unemployment Rate (Mar)", "High"),
    # --- BLS: Consumer Price Index
    ("2026-12-10", "08:30", "CPI m/m and y/y (Nov)", "High"),
    ("2027-01-13", "08:30", "CPI m/m and y/y (Dec)", "High"),
    ("2027-02-11", "08:30", "CPI m/m and y/y (Jan)", "High"),
    ("2027-03-10", "08:30", "CPI m/m and y/y (Feb)", "High"),
    ("2027-04-13", "08:30", "CPI m/m and y/y (Mar)", "High"),
    # --- BLS: Producer Price Index
    ("2026-12-15", "08:30", "PPI m/m (Nov)", "Medium"),
    ("2027-01-14", "08:30", "PPI m/m (Dec)", "Medium"),
    # --- Federal Reserve: FOMC policy decisions (statement 2:00 pm, press conference 2:30 pm)
    ("2026-12-09", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-01-27", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-03-17", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-04-28", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-06-09", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-07-28", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-09-15", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
    ("2027-11-17", "14:00", "FOMC Statement & Federal Funds Rate", "High"),
]


def as_feed() -> list[dict]:
    """The schedule in the same shape as the ForexFactory feed."""
    out = []
    for day, hm, title, impact in EVENTS:
        when = datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=NY)
        out.append({"title": title, "country": "USD", "date": when.isoformat(), "impact": impact,
                    "forecast": "", "previous": ""})
    return out
