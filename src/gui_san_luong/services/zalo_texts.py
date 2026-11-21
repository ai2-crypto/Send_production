"""Vietnamese sentences explaining a Zalo error to the operator, for any front end."""

from gui_san_luong.services.output_dispatch import (
    RecipientResult,
    RecipientState,
    SendOutcome,
)
from gui_san_luong.services.production_review import count_text
from gui_san_luong.services.zalo_codes import LinkProposal, ProposalStatus
from gui_san_luong.zalo.errors import (
    ZaloAuthError,
    ZaloConfigurationError,
    ZaloError,
    ZaloRateLimitError,
    ZaloUnavailableError,
    error_for,
)

RECIPIENT_BLOCKED_CODES = frozenset({-213, -217, -227, -230, -232, -244})
QUOTA_CODES = frozenset({-211, -218})
NIGHT_CODE = -234


def send_failure_text(code: int | None, message: str | None) -> str:
    """Why Zalo did not take a message, and what the operator can do about it."""
    if code is None:
        return "Không kết nối được Zalo nên tin chưa được gửi. Có thể gửi lại."
    if code in RECIPIENT_BLOCKED_CODES:
        return (
            f"Zalo không cho gửi đến người này (mã lỗi {code}). Thường là do họ đã quá "
            "7 ngày không tương tác với OA, chưa quan tâm hoặc đã chặn OA. Hãy nhờ họ nhắn "
            "một tin đến OA rồi thử lại."
        )
    if code in QUOTA_CODES:
        return f"Đã hết hạn mức gửi tin (mã lỗi {code}). Đừng thử lại ngay."
    if code == NIGHT_CODE:
        return "Zalo không cho gửi loại tin này từ 22 giờ đến 7 giờ. Hãy gửi sau 7 giờ."
    error = error_for(code, message or "")
    if isinstance(error, ZaloAuthError):
        return (
            "Access token không hợp lệ hoặc đã hết hạn. Bấm “Làm mới ngay” ở tab “Tin nhắn "
            "Zalo (thử)”, hoặc dán token mới từ API Explorer."
        )
    if isinstance(error, ZaloRateLimitError):
        return "Zalo báo gọi quá nhiều trong một phút. Hãy đợi một phút rồi thử lại."
    if isinstance(error, ZaloConfigurationError):
        return (
            f"Ứng dụng hoặc OA chưa đủ quyền gửi tin (mã lỗi {code}). Hãy kiểm tra quyền "
            "“Gửi tin và thông báo qua OA” và gói dịch vụ của OA."
        )
    return f"Zalo từ chối tin (mã lỗi {code}): {message or ''}"


def error_text(error: ZaloError) -> str:
    if isinstance(error, ZaloAuthError):
        return (
            "Access token không hợp lệ hoặc đã hết hạn. Bấm “Làm mới ngay” ở tab “Tin nhắn "
            "Zalo (thử)”, hoặc dán token mới từ API Explorer."
        )
    if isinstance(error, ZaloRateLimitError):
        return "Zalo báo gọi quá nhiều trong một phút. Hãy đợi một phút rồi thử lại."
    if isinstance(error, ZaloConfigurationError):
        return (
            f"Ứng dụng hoặc OA chưa đủ quyền (mã lỗi {error.code}). Hãy kiểm tra ứng dụng "
            "đã được cấp “Quyền quản lý tin nhắn người dùng” và gói dịch vụ của OA."
        )
    if isinstance(error, ZaloUnavailableError):
        return "Không kết nối được Zalo hoặc Zalo đang lỗi. Hãy thử lại sau ít phút."
    code = f" (mã lỗi {error.code})" if error.code is not None else ""
    return f"Zalo từ chối yêu cầu{code}: {error.message}"


RECIPIENT_STATE_TEXTS = {
    RecipientState.READY: "📤 Sẽ gửi",
    RecipientState.NOT_LINKED: "🔗 Chưa nối Zalo",
    RecipientState.NO_INTERACTION: "🚫 Chưa tương tác với OA: không gửi",
    RecipientState.NEEDS_INTERACTION: "⛔ Quá 7 ngày chưa tương tác: Zalo sẽ từ chối",
    RecipientState.ALREADY_SENT: "✅ Đã gửi đúng nội dung này",
    RecipientState.UNKNOWN_BEFORE: "❓ Lần trước không rõ đã gửi chưa",
}


def send_result_text(result: RecipientResult) -> str:
    if result.outcome is SendOutcome.SENT:
        return f"{count_text(len(result.message_ids))} tin"
    if result.outcome is SendOutcome.UNKNOWN:
        return (
            "Mất kết nối hoặc Zalo lỗi khi gửi. Đừng gửi lại ngay: xem hội thoại với "
            "người này trước."
        )
    if result.outcome is SendOutcome.STOPPED:
        return "Không gửi vì lô đã dừng ở lỗi phía trên."
    return send_failure_text(result.error_code, result.error_message)


def proposal_status_text(proposal: LinkProposal) -> str:
    match proposal.status:
        case ProposalStatus.NEW:
            return "Mới"
        case ProposalStatus.REPLACES_ACCOUNT:
            return f"Mã này đang nối Zalo khác ({proposal.replaced}): sẽ thay"
        case ProposalStatus.MOVES_ACCOUNT:
            return f"Zalo này đang nối mã {proposal.replaced}: sẽ chuyển sang mã mới"
        case ProposalStatus.CLAIMED_BY_SEVERAL:
            return "Nhiều Zalo cùng nhắn mã này: chỉ chọn một"
        case ProposalStatus.NOT_IN_LIST:
            return "Mã không có trong danh sách nhân viên"
        case ProposalStatus.NO_LIST:
            return "Chưa tải file sản lượng nên chưa đối chiếu được"
        case ProposalStatus.UNCHANGED:
            return "Đã nối đúng"
