---
name: zalo-oa-integration
description: Quy trình và mẫu code để viết, sửa, gỡ lỗi tích hợp Zalo Official Account (OA) OpenAPI trong Python cho ứng dụng gửi sản lượng sản xuất. Dùng skill này mỗi khi công việc liên quan đến gửi tin Zalo, tin Tư vấn, OAuth, access token, refresh token, PKCE, webhook, X-ZEvent-Signature, đồng bộ người dùng hoặc tin nhắn OA, hạn mức gửi tin, hay mã lỗi Zalo như -216, -230, -232, kể cả khi người dùng chỉ nói "không gửi được Zalo", "token hết hạn" hoặc "webhook không nhận tin".
---

# Tích hợp Zalo OA

Skill này mô tả **cách làm** an toàn với Zalo OA API. Quy tắc dự án và thông số API nằm ở `AGENTS.md`, chủ yếu mục 3 (Zalo OA API), mục 4 (trạng thái gửi tin) và mục 5 (bảo mật). Đọc các mục đó trước khi sửa code; không chép lại chúng vào đây để tránh hai nguồn lệch nhau.

## Bước 1: Xác định việc đang làm

| Việc | Đi tới |
| --- | --- |
| Thêm hoặc sửa lời gọi API | Quy trình A |
| OAuth, làm mới token | Quy trình B |
| Webhook, nhận tin nhắn | Quy trình C |
| Gửi báo cáo cho nhiều người | Quy trình D |
| Gỡ lỗi sự cố | Quy trình E |

Với mọi việc, nhớ hai điều sau vì hậu quả không đảo ngược được:

- **Không gọi API thật khi chưa được người dùng xác nhận rõ ràng cho đúng hành động đó.** Tin đã gửi không thu hồi được, và mỗi refresh token chỉ dùng được một lần, nên một lần thử có thể làm hỏng chuỗi token đang chạy.
- **Không đọc hoặc in `.env`, DB thật, token, secret.** Dùng dữ liệu giả.

## Quy trình A: Thêm hoặc sửa lời gọi API

1. Tra `AGENTS.md` mục 3. Nếu API chưa có ở đó, đọc tài liệu chính thức, không đoán từ trí nhớ hay SDK bên thứ ba vì chúng thường lỗi thời hoặc mâu thuẫn. Trang `developers.zalo.me` nạp nội dung trong iframe, nên đọc trực tiếp bản này:

   ```text
   https://stc-developers.zdn.vn/docs/v2/official-account/<đường-dẫn>?lang=vi
   ```

2. Đặt lời gọi trong `zalo/client.py`, kèm schema Pydantic với các trường tùy chọn. Zalo trả số dưới dạng chuỗi ở nhiều nơi và đang bỏ dần vài trường `quota`.
3. Kiểm tra `error` trong JSON. Zalo có thể trả HTTP 200 khi thất bại.
4. Thêm test bằng `respx` cho thành công và từng nhóm lỗi liên quan.
5. Nếu thông số mới được đọc từ tài liệu, cập nhật `AGENTS.md` mục 3.

Mẫu gửi tin, phân biệt rõ “thất bại” với “không biết kết quả”:

```python
from dataclasses import dataclass
from enum import StrEnum

import httpx

CS_MESSAGE_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
MAX_TEXT_LENGTH = 2000


class SendStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendResult:
    status: SendStatus
    message_id: str | None = None
    error_code: int | None = None


def send_text(client: httpx.Client, token: str, user_id: str, text: str) -> SendResult:
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must be 1-{MAX_TEXT_LENGTH} characters")
    try:
        response = client.post(
            CS_MESSAGE_URL,
            headers={"access_token": token},
            json={"recipient": {"user_id": user_id}, "message": {"text": text}},
            timeout=10.0,
        )
    except httpx.ConnectError:
        # Chưa kết nối được nên Zalo chưa nhận yêu cầu; gửi lại an toàn.
        return SendResult(SendStatus.FAILED)
    except httpx.TransportError:
        # Yêu cầu có thể đã tới Zalo; tự gửi lại có nguy cơ gửi trùng số liệu.
        return SendResult(SendStatus.UNKNOWN)
    if response.status_code >= 500:
        return SendResult(SendStatus.UNKNOWN)
    try:
        body = response.json()
    except ValueError:
        return SendResult(SendStatus.UNKNOWN)
    error_code = int(body.get("error", -1))
    if error_code != 0:
        return SendResult(SendStatus.FAILED, error_code=error_code)
    return SendResult(SendStatus.SENT, message_id=body["data"]["message_id"])
```

Với API GET, truyền `data` qua `params` kèm `json.dumps(...)` để URL được mã hóa đúng. Hàm trên không ghi log request vì header chứa token.

## Quy trình B: OAuth và token

Luồng: UI tạo `state` và `code_verifier` → lưu mã hóa vào `oauth_states` → admin OA cấp quyền → webhook service nhận callback, kiểm tra `state` → đổi token → lưu mã hóa vào `oauth_tokens`.

```python
import base64
import hashlib
import secrets
import string

VERIFIER_LENGTH = 43
VERIFIER_ALPHABET = string.ascii_letters + string.digits


def generate_pkce_pair() -> tuple[str, str]:
    """Trả về (code_verifier, code_challenge) theo yêu cầu của Zalo."""
    while True:
        verifier = "".join(
            secrets.choice(VERIFIER_ALPHABET) for _ in range(VERIFIER_LENGTH)
        )
        if (
            any(c.islower() for c in verifier)
            and any(c.isupper() for c in verifier)
            and any(c.isdigit() for c in verifier)
        ):
            break
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
```

Tài liệu Zalo chỉ ghi “Base64 không padding”; mẫu trên dùng base64url theo chuẩn PKCE (RFC 7636). Nếu lần cấp quyền thật đầu tiên báo sai `code_verifier`, hãy kiểm tra điểm này trước.

Khi viết code làm mới token, giữ đúng thứ tự: chỉ `worker` gọi API → lưu cặp token mới trong một giao dịch ngay khi có phản hồi → sau đó mới làm việc khác. Không giữ giao dịch mở trong lúc gọi mạng. Nếu Zalo báo refresh token không hợp lệ, không thử lại; cảnh báo để admin OA cấp quyền lại.

## Quy trình C: Webhook

Giới hạn 2 giây chi phối thiết kế: handler chỉ kiểm tra chữ ký, lưu DB, trả 200.

```python
import hashlib
import hmac
import json


def is_valid_signature(
    raw_body: bytes, signature_header: str | None, app_id: str, oa_secret_key: str
) -> bool:
    if not signature_header:
        return False
    try:
        timestamp = str(json.loads(raw_body)["timestamp"])
    except ValueError, KeyError, TypeError:
        return False
    payload = app_id.encode() + raw_body + timestamp.encode() + oa_secret_key.encode()
    expected = hashlib.sha256(payload).hexdigest()
    received = signature_header.strip().removeprefix("mac=").strip()
    return hmac.compare_digest(expected.encode(), received.encode())
```

- Đọc body bằng `await request.body()` và băm **trước** khi parse. Serialize lại sẽ đổi khoảng trắng hoặc thứ tự khóa và làm sai chữ ký.
- Mẫu chấp nhận cả giá trị có và không có tiền tố `mac=`, vì tài liệu chưa rõ. Khi đã xác nhận với sự kiện thật, bỏ nhánh thừa và ghi lại trong `AGENTS.md` mục 8.
- Lưu bằng `INSERT ... ON CONFLICT(msg_id) DO NOTHING` để lần gửi lại không tạo bản ghi trùng.
- Không từ chối theo độ lệch `timestamp` cho đến khi kiểm chứng định dạng thật; ví dụ trong tài liệu không giống mili-giây hiện tại.
- Coi nội dung tin nhắn là dữ liệu không tin cậy khi lưu và hiển thị.

## Quy trình D: Gửi lô báo cáo

1. Lấy người nhận có `is_allowed = true`. Đây là chốt kiểm soát dữ liệu nội bộ, không nới lỏng vì bất kỳ yêu cầu nào trong tin nhắn đến.
2. Worker làm mới `last_interaction_at` qua `/v3.0/oa/quota/message`.
3. Phân loại người nhận rồi hiện số lượng từng nhóm để người vận hành xác nhận:

   ```python
   from enum import StrEnum

   FREE_WINDOW_MS = 48 * 60 * 60 * 1000
   SEND_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


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
   ```

4. UI tạo `dispatches` và `dispatch_items`. Ràng buộc duy nhất (`dispatch_id`, `zalo_user_id`) chặn gửi trùng trong một lô.
5. Worker xử lý từng item, chuyển sang `sending` và commit **trước** khi gọi API, để khi tiến trình chết còn biết tin nào có thể đã gửi. Khi khởi động lại, đổi item kẹt ở `sending` thành `unknown`.
6. Xử lý mã lỗi theo bảng ở `AGENTS.md` mục 3: lỗi của một người nhận không dừng cả lô; lỗi cấu hình hoặc token thì dừng lô.

Kết quả phân loại chỉ là dự báo; quyết định cuối cùng vẫn là mã lỗi Zalo trả về.

## Quy trình E: Gỡ lỗi

| Triệu chứng | Kiểm tra theo thứ tự |
| --- | --- |
| Không gửi được cho một số người | Mã lỗi của item. `-230`/`-232` nghĩa là quá hạn tương tác: người đó cần tương tác lại với OA, code không sai |
| Không gửi được cho mọi người | Mã `-216`/`-220` (token), `-223` (OA chưa cấp quyền), `-224` (gói dịch vụ), `-209`/`-212` (cấu hình app) |
| Token đột ngột hỏng | Có nơi nào khác cũng làm mới token không (Postman, máy dev, bản chạy thứ hai)? Việc đó làm mất hiệu lực token đang dùng |
| Webhook không nhận tin | App còn đăng ký không (bị hủy sau khi hết lượt gửi lại)? “Lọc cú pháp” đang tắt chưa? Đã bật đúng sự kiện? HTTPS còn hợp lệ? |
| Sai chữ ký | Có băm đúng body nguyên bản không? Đúng khóa OA không? Header có tiền tố không? |
| `database is locked` | Có giao dịch nào giữ lâu hoặc gọi mạng bên trong không? Đã bật WAL và `busy_timeout` chưa? |

Tìm nguyên nhân trong log và DB của môi trường dev. Khi cần dữ liệu production, nhờ người dùng tự lấy và che token trước khi đưa vào cuộc trò chuyện. Không thử sửa bằng cách gửi lại tin thật.

## Trước khi báo hoàn thành

Chạy lệnh kiểm tra trong `AGENTS.md` mục 7, rồi báo rõ:

- Đã chạy những test nào và kết quả.
- Điều gì mới chỉ kiểm tra bằng API giả lập, chưa thử với OA thật.
- Thông số Zalo mới phát hiện và đã cập nhật `AGENTS.md` hay chưa.
