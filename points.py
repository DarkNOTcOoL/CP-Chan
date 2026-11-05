"""
points.py — Custom point-weighting rules for Codeforces problem ratings.

Rating tiers
------------
<= 900         →  5 pts   (800–900 bracket)
901  – 999     →  5 pts   (between brackets; treated as 800–900)
1000 – 1099    →  7 pts
1100 – 1199    →  8 pts
1200           → 10 pts
1201 – 1999    → 15 pts
2000+          → 50 pts
Unrated / None →  5 pts   (default)
"""

from __future__ import annotations


def calculate_points(rating: int | None) -> int:
    """Return the points awarded for a problem of the given `rating`."""
    if rating is None:
        return 5
    if rating <= 999:   # covers 800–900 and the 901–999 edge case
        return 5
    if rating <= 1099:  # 1000–1099
        return 7
    if rating <= 1199:  # 1100–1199
        return 8
    if rating == 1200:  # exactly 1200
        return 10
    if rating <= 1999:  # 1201–1999
        return 15
    return 50           # 2000+


# Human-readable label for the tier a rating falls into
def rating_tier_label(rating: int | None) -> str:
    if rating is None:
        return "Unrated"
    if rating <= 999:
        return "800–999"
    if rating <= 1099:
        return "1000–1099"
    if rating <= 1199:
        return "1100–1199"
    if rating == 1200:
        return "1200"
    if rating <= 1999:
        return "1201–1999"
    return "2000+"

