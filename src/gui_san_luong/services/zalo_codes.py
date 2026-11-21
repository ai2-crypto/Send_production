"""Read employee codes that people texted to the OA, and propose them as Zalo links.

Anyone can text the OA any code, and a link decides who receives that employee's figures, so
codes are only proposed: the operator compares names and ticks what to apply. A proposal that
would take a code or an account away from someone is never ticked by default.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from gui_san_luong.db.zalo_links import LinkRecord
from gui_san_luong.domain.send_window import FREE_WINDOW_MS
from gui_san_luong.services.worker_output import EmployeeIdentity
from gui_san_luong.services.zalo_links import (
    InvalidLinksError,
    LinkChanges,
    LinkRow,
    save_links,
)
from gui_san_luong.services.zalo_messages import MessageArchive
from gui_san_luong.zalo.schemas import MessageSource

# Employees write their code as six digits, zero-padded: 000020, 002960.
CODE_PATTERN = re.compile(r"(?<!\d)\d{6}(?!\d)")


@dataclass(frozen=True)
class CodeClaim:
    """The newest code a Zalo user texted, with the message it came from."""

    user_id: str
    display_name: str | None
    code: str
    sent_ms: int
    message: str


def find_code(text: str | None) -> str | None:
    """The one six-digit code in a message; None when there is none or several differ."""
    codes = set(CODE_PATTERN.findall(text or ""))
    return codes.pop() if len(codes) == 1 else None


def latest_claims(archive: MessageArchive) -> list[CodeClaim]:
    claims = []
    for conversation in archive.conversations:
        for message in sorted(
            conversation.messages, key=lambda m: m.time, reverse=True
        ):
            if message.src is not MessageSource.FROM_USER:
                continue
            if code := find_code(message.message):
                claims.append(
                    CodeClaim(
                        conversation.user_id,
                        conversation.display_name,
                        code,
                        message.time,
                        message.message or "",
                    )
                )
                break
    return sorted(claims, key=lambda claim: claim.sent_ms, reverse=True)


class ProposalStatus(StrEnum):
    NEW = "new"
    UNCHANGED = "unchanged"
    REPLACES_ACCOUNT = "replaces_account"
    MOVES_ACCOUNT = "moves_account"
    CLAIMED_BY_SEVERAL = "claimed_by_several"
    NOT_IN_LIST = "not_in_list"
    NO_LIST = "no_list"


# Only a plain new link is ticked without the operator looking at it.
SAFE_STATUSES = frozenset({ProposalStatus.NEW})


@dataclass(frozen=True)
class LinkProposal:
    claim: CodeClaim
    # The code as the employee list writes it ("20" for a texted "000020").
    employee_code: str
    employee_name: str | None
    status: ProposalStatus
    # The account or code this link would replace, to show the operator.
    replaced: str | None = None

    @property
    def selected_by_default(self) -> bool:
        return self.status in SAFE_STATUSES


def _number(code: str) -> str:
    return code.lstrip("0") or "0"


def _match(code: str, roster: dict[str, EmployeeIdentity]) -> EmployeeIdentity | None:
    """Files write the same code as 20, 0020 or 000020: compare the number itself."""
    if code in roster:
        return roster[code]
    matches = [e for c, e in roster.items() if _number(c) == _number(code)]
    return matches[0] if len(matches) == 1 else None


def plan_proposals(
    claims: Iterable[CodeClaim],
    stored: Iterable[LinkRecord],
    roster: Iterable[EmployeeIdentity],
) -> list[LinkProposal]:
    claims = list(claims)
    employees = {employee.employee_code: employee for employee in roster}
    by_code = {link.employee_code: link for link in stored}
    by_account = {link.zalo_user_id: link for link in by_code.values()}
    # Linked employees count as known even when today's files do not show them.
    known = {
        code: EmployeeIdentity(code, link.employee_name, ())
        for code, link in by_code.items()
    }
    known.update(employees)

    resolved: list[tuple[CodeClaim, str, str | None]] = []
    for claim in claims:
        employee = _match(claim.code, known)
        code = employee.employee_code if employee else claim.code
        resolved.append((claim, code, employee.employee_name if employee else None))
    claimants: defaultdict[str, set[str]] = defaultdict(set)
    for claim, code, _ in resolved:
        claimants[code].add(claim.user_id)

    proposals = []
    for claim, code, name in resolved:
        current = by_code.get(code)
        owned = by_account.get(claim.user_id)
        replaced = None
        if current and current.zalo_user_id == claim.user_id:
            status = ProposalStatus.UNCHANGED
        elif len(claimants[code]) > 1:
            status = ProposalStatus.CLAIMED_BY_SEVERAL
        elif current:
            status, replaced = ProposalStatus.REPLACES_ACCOUNT, current.zalo_user_id
        elif owned:
            status, replaced = ProposalStatus.MOVES_ACCOUNT, owned.employee_code
        elif not employees:
            status = ProposalStatus.NO_LIST
        elif name is None:
            status = ProposalStatus.NOT_IN_LIST
        else:
            status = ProposalStatus.NEW
        proposals.append(LinkProposal(claim, code, name, status, replaced))
    return proposals


def apply_proposals(
    db_path: Path,
    stored: Sequence[LinkRecord],
    proposals: Iterable[LinkProposal],
    actor: str,
) -> LinkChanges:
    """Add the chosen links to the saved ones, through the same checks as typed links."""
    proposals = list(proposals)
    accounts: defaultdict[str, set[str]] = defaultdict(set)
    for proposal in proposals:
        accounts[proposal.employee_code].add(proposal.claim.user_id)
    if clashes := sorted(code for code, users in accounts.items() if len(users) > 1):
        # Applying both would keep whichever came last: the operator must pick one.
        raise InvalidLinksError(
            [f"Mã {code}: nhiều Zalo cùng được chọn, chỉ chọn một" for code in clashes]
        )
    links = {link.employee_code: link for link in stored}
    for proposal in proposals:
        user_id = proposal.claim.user_id
        # One account belongs to one employee: a moved account leaves its old code.
        for code, link in list(links.items()):
            if link.zalo_user_id == user_id:
                del links[code]
        name = proposal.employee_name or (
            links[proposal.employee_code].employee_name
            if proposal.employee_code in links
            else proposal.employee_code
        )
        links[proposal.employee_code] = LinkRecord(
            proposal.employee_code, name, user_id
        )
    rows = [
        LinkRow(link.employee_code, link.employee_name, link.zalo_user_id)
        for link in links.values()
    ]
    return save_links(db_path, rows, actor)


@dataclass(frozen=True)
class ActiveUser:
    """Someone who texted the OA recently enough to be messaged for free."""

    user_id: str
    display_name: str | None
    last_interaction_ms: int

    @property
    def free_until_ms(self) -> int:
        return self.last_interaction_ms + FREE_WINDOW_MS


def free_window_users(archive: MessageArchive, now: datetime) -> list[ActiveUser]:
    """Users whose last message to the OA is under 48 hours old, the soonest to expire first."""
    now_ms = int(now.timestamp() * 1000)
    users = []
    for conversation in archive.conversations:
        last = max(
            (m.time for m in conversation.messages if m.src is MessageSource.FROM_USER),
            default=None,
        )
        if last is not None and now_ms - last < FREE_WINDOW_MS:
            users.append(
                ActiveUser(conversation.user_id, conversation.display_name, last)
            )
    return sorted(users, key=lambda user: user.last_interaction_ms)
