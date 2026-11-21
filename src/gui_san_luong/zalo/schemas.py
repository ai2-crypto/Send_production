"""Shapes of Zalo responses. Fields Zalo may leave out are optional.

User and OA ids are large numbers that Zalo sends sometimes as numbers and sometimes as
strings; they are kept as strings so no digit is ever lost or reformatted.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessageSource(IntEnum):
    FROM_OA = 0
    FROM_USER = 1


class ZaloMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    message_id: str
    src: MessageSource
    # Milliseconds since the epoch.
    time: int
    type: str | None = None
    message: str | None = None
    from_id: str
    to_id: str
    from_display_name: str | None = None
    to_display_name: str | None = None
    url: str | None = None
    thumb: str | None = None
    description: str | None = None
    links: list[Any] | None = None
    location: Any = None

    @field_validator("message_id", "from_id", "to_id", mode="before")
    @classmethod
    def _id_as_text(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) else value

    @field_validator("time", mode="before")
    @classmethod
    def _time_as_number(cls, value: Any) -> Any:
        return int(value) if isinstance(value, str) and value.isdigit() else value

    @property
    def user_id(self) -> str:
        """The person on the other side of the OA, whichever way the message went."""
        return self.from_id if self.src is MessageSource.FROM_USER else self.to_id

    @property
    def user_display_name(self) -> str | None:
        if self.src is MessageSource.FROM_USER:
            return self.from_display_name
        return self.to_display_name


class TokenPair(BaseModel):
    """A new OA token pair. `expires_in` is a string of seconds in the documented answer."""

    model_config = ConfigDict(frozen=True, extra="ignore", hide_input_in_errors=True)

    access_token: str = Field(min_length=1, repr=False)
    refresh_token: str = Field(min_length=1, repr=False)
    expires_in: int = Field(gt=0)

    @field_validator("expires_in", mode="before")
    @classmethod
    def _seconds_as_number(cls, value: Any) -> Any:
        return int(value) if isinstance(value, str) and value.isdigit() else value


class SendStatus(StrEnum):
    SENT = "sent"
    # Zalo refused it, or it never left: sending again is safe.
    FAILED = "failed"
    # It may have been delivered; sending again could duplicate it.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    message_id: str | None = None
    error_code: int | None = None
    error_message: str | None = None
    # Which allowance paid for it, e.g. "reply" inside the 48-hour window.
    quota_type: str | None = None
