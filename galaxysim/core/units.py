"""Human-readable magnitudes.

Populations run to the billions and stockpiles to the billions of tonnes, so
almost nothing in this game is legible as a raw number. Formatting lives here
rather than in the CLI because the engine writes these figures into event log
messages too, and "18145461021 people" in a log line is no more readable than it
is in a table.
"""

from __future__ import annotations


def format_count(value: float) -> str:
    """Population, tonnage and similar magnitudes in human units."""
    if value >= 1e12:
        return f"{value / 1e12:.2f}T"
    if value >= 1e9:
        return f"{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{value / 1e6:.2f}M"
    if value >= 1e3:
        return f"{value / 1e3:.1f}k"
    return f"{value:,.0f}"
