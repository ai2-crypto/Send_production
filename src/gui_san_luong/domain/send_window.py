"""Whether the OA may send a consultation message, judged from the user's last interaction.

Zalo sends free within 48 hours of the user's last interaction, may charge up to 7 days, and
refuses after that. This is only a forecast: Zalo's error code has the final word.
"""

from enum import StrEnum

HOUR_MS = 60 * 60 * 1000
FREE_WINDOW_MS = 48 * HOUR_MS
SEND_WINDOW_MS = 7 * 24 * HOUR_MS


class Eligibility(StrEnum):
    FREE = "free"
    PAID = "paid"
    NEEDS_INTERACTION = "needs_interaction"


def classify_eligibility(last_interaction_ms: int | None, now_ms: int) -> Eligibility:
    if last_interaction_ms is None:
        return Eligibility.NEEDS_INTERACTION
    age_ms = now_ms - last_interaction_ms
    if age_ms < FREE_WINDOW_MS:
        return Eligibility.FREE
    if age_ms <= SEND_WINDOW_MS:
        return Eligibility.PAID
    return Eligibility.NEEDS_INTERACTION
