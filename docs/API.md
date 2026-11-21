# Gửi sản lượng API — đặc tả tích hợp

API HTTP/JSON làm được mọi việc của trang web Streamlit: đọc file Excel sản lượng, kiểm tra, xem trước và gửi cho từng nhân viên qua Zalo OA, nối mã nhân viên với Zalo, đọc tin nhắn OA và quản lý token.

- **Đặc tả máy đọc được:** [`openapi.json`](openapi.json) (OpenAPI 3.1, sinh từ code). Dùng để sinh client, ví dụ `npx openapi-typescript docs/openapi.json -o api.d.ts`.
- **Tài liệu tương tác khi API đang chạy:** `http://<máy-chủ>:8800/api/v1/docs` (Swagger UI) và `/api/v1/redoc`.
- Sinh lại `openapi.json` sau khi sửa API: `uv run python -c "import json; from gui_san_luong.api.app import create_app; open('docs/openapi.json','w',encoding='utf-8').write(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + '\n')"`.

## 1. Kết nối

| Mục | Giá trị |
| --- | --- |
| Gốc API | `http://<IP-NAS>:8800/api/v1` (chạy trên máy: `uv run python -m uvicorn gui_san_luong.api.app:app --port 8800`) |
| Định dạng | JSON UTF-8; riêng `POST /imports` là `multipart/form-data`, ảnh trả `image/png` hoặc `image/jpeg` |
| Xác thực | **Không có.** Ai vào được cổng 8800 đều gửi được tin Zalo: chỉ dùng trong mạng nội bộ, không mở cổng ra Internet. Frontend phải tự kiểm soát người dùng của mình |
| CORS | Biến `API_CORS_ORIGINS` trong `.env`, các origin cách nhau dấu phẩy (ví dụ `http://192.168.1.20:3000`). Bỏ trống = cho mọi origin |
| Ngày | `YYYY-MM-DD` (ngày sản xuất, không có giờ) |
| Thời điểm | ISO 8601 có múi giờ UTC, ví dụ `2026-11-21T01:39:00Z`. Hiển thị theo giờ Việt Nam (UTC+7) |
| Số lượng | Số thực (`60.0`); frontend tự định dạng, quy ước của dự án là dấu chấm ngăn nghìn |
| Mã nhân viên | Luôn là chuỗi (`"000020"` khác `"20"` khi hiển thị nhưng API so theo đúng chuỗi trong file) |
| Zalo user id | Luôn là chuỗi chữ số (số lớn, không đổi sang number trong JavaScript) |

## 2. Lỗi

Mọi lỗi có cùng dạng:

```json
{ "error": { "code": "confirmation_stale", "message": "Câu tiếng Việt hiện cho người dùng.", "details": null } }
```

| HTTP | `code` | Khi nào |
| --- | --- | --- |
| 404 | `not_found` | Không có lượt tải, file, nhân viên, ảnh… Lượt tải hết hạn cũng trả 404: tải file lên lại |
| 422 | `invalid_request` | Dữ liệu gửi lên sai; `details` là danh sách `{field, problem}` |
| 422 | `invalid_links` | Bảng nối Zalo có dòng sai; `details` là danh sách câu mô tả từng dòng |
| 409 | `confirmation_required` | Gửi tin thử mà `confirm` khác `true` |
| 409 | `confirmation_stale` | `confirmation` không khớp kế hoạch gửi hiện tại: lấy kế hoạch mới |
| 409 | `token_missing` | Chưa có access token Zalo |
| 409 | `token_setup` | `.env` thiếu `ZALO_APP_ID` / `ZALO_SECRET_KEY` / `TOKEN_ENCRYPTION_KEY`, hoặc khóa sai |
| 409 / 503 | `image_not_available` | Không vẽ hoặc không gửi được ảnh (thiếu phông chữ, hết lượt tải ảnh tháng) |
| 401 | `zalo_auth` | Zalo báo access token sai/hết hạn; `details.zalo_code` là mã lỗi Zalo |
| 429 | `zalo_rate_limited` | Zalo báo gọi quá nhiều trong phút: đợi một phút |
| 502 | `zalo_error` | Zalo từ chối vì lý do khác |
| 503 | `storage` | Không đọc/ghi được SQLite trên máy chủ (thường do quyền thư mục `data`) |

Luôn hiện `error.message` cho người dùng; nó đã giải thích nguyên nhân và cách xử lý.

## 3. Luồng làm việc (giống 4 bước trên web)

```text
1. POST /imports (các file Excel)               → import_id, files[], days[]
   GET  /imports/{id}/files/{file_id}            → cách đọc từng file, nếu cần sửa:
   PUT  /imports/{id}/files/{file_id}/settings   → đọc lại file với loại/sheet/dòng/cột khác
2. Chọn một ngày trong days[]
3. GET  /imports/{id}/issues?day=…               → điểm cần kiểm tra (error/warning/info)
4. GET  /imports/{id}/days/{day}                 → nhân viên, sản lượng, tình trạng Zalo, số liệu vừa sửa
   GET  …/employees/{code}/messages | /images    → xem trước đúng nội dung sẽ gửi
   POST /imports/{id}/days/{day}/send-plan       → ai sẽ được gửi, ai bị bỏ qua, mã xác nhận
   POST /imports/{id}/days/{day}/sends           → gửi (kèm mã xác nhận)
```

**Lượt tải file** (`import_id`) được giữ trong bộ nhớ máy chủ 8 giờ kể từ lần dùng cuối, tối đa 20 lượt; khởi động lại API thì mất (tải lại nhanh vì kết quả đọc được lưu đệm theo nội dung file). Mỗi lượt tối đa 20 file, mỗi file tối đa bằng giới hạn của trang web. File trùng nội dung chỉ đọc một lần, bản trùng nằm trong `skipped`.

Mỗi lần đọc file, API so với lần tải trước của cùng file: số dòng đổi nằm ở `changed_rows`, chi tiết ở `GET /output-changes` và trong `changes` của từng nhân viên.

## 4. Gửi tin: kế hoạch và xác nhận

Tin đã gửi không thu hồi được, nên gửi luôn qua hai bước:

1. `POST …/send-plan` với `{ "style": "text" | "image", "employee_codes": [...], "resend_unknown": false }`. Hiện cho người dùng `recipients[]` (trạng thái từng người), `ready_count`, `parts_to_send`, `paid_count`, và với ảnh là `image_quota`.
2. Người dùng xác nhận, frontend gọi `POST …/sends` với **cùng** `style`, `employee_codes`, `resend_unknown` và `confirmation` lấy từ bước 1.

`confirmation` là mã băm của đúng những người sẵn sàng gửi và nội dung của họ. Nếu giữa hai bước có người được chọn thêm/bớt, file bị đọc lại với số khác, hay một người vừa được gửi ở nơi khác, API trả `409 confirmation_stale` và **không gửi gì**: lấy kế hoạch mới, cho người dùng xem lại. Không tự động gọi lại `sends` khi gặp lỗi này.

Chỉ người có `state = ready` được gửi. Hai dạng `text` và `image` được theo dõi riêng: đã gửi chữ vẫn gửi ảnh được.

### Trạng thái người nhận (`state`)

| Giá trị | Ý nghĩa |
| --- | --- |
| `ready` | Sẽ gửi |
| `not_linked` | Mã nhân viên chưa nối Zalo |
| `no_interaction` | Chưa thấy tin nào người này nhắn cho OA (hoặc chưa quét tin nhắn): không gửi |
| `needs_interaction` | Quá 7 ngày chưa nhắn OA: Zalo sẽ từ chối |
| `already_sent` | Đã nhận đúng nội dung này |
| `unknown_before` | Lần trước không rõ đã gửi chưa; chỉ gửi lại khi `resend_unknown = true` (có thể nhận trùng) |

`state_text` là câu tiếng Việt kèm biểu tượng, dùng thẳng để hiển thị.

### Khung gửi (`window`)

| Giá trị | Ý nghĩa |
| --- | --- |
| `free` | Trong 48 giờ sau lần nhắn cuối: miễn phí |
| `paid` | 48 giờ – 7 ngày: gửi được nhưng Zalo có thể tính phí (cảnh báo người dùng trước khi xác nhận) |
| `needs_interaction` | Quá 7 ngày |
| `null` | Chưa biết lần nhắn cuối |

### Kết quả gửi (`outcome`)

| Giá trị | Ý nghĩa |
| --- | --- |
| `sent` | Zalo đã nhận |
| `failed` | Zalo từ chối hoặc không kết nối được; `detail` nói lý do và có nên gửi lại không |
| `unknown` | Mất kết nối giữa chừng: Zalo có thể đã nhận. **Không gửi lại ngay**; xem hội thoại trước |
| `stopped` | Không gửi vì lô dừng ở lỗi phía trước (token, hạn mức, cấu hình) |

## 5. Danh sách endpoint

Tất cả có tiền tố `/api/v1`. Chi tiết từng trường: `openapi.json` hoặc `/api/v1/docs`.

### Chung

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| GET | `/health` | `{"status": "ok"}` |
| GET | `/templates` | Các loại báo cáo, trường dữ liệu (`key`, `label`, `required`) |

### Sản lượng (bước 1–4)

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| POST | `/imports` | Tải file (`multipart/form-data`, trường `files`, lặp lại cho nhiều file). Trả `201` |
| GET | `/imports/{import_id}` | Tình trạng lượt tải: `files[]` (`status`: `ready` / `check` / `excluded`, `status_text`), `skipped[]`, `days[]`, `changed_rows` |
| DELETE | `/imports/{import_id}` | Bỏ lượt tải (`204`) |
| GET | `/imports/{import_id}/files/{file_id}` | Cách đọc: `sheets`, `start_row`, `fields[]` (cột tự nhận `detected_column`, đang dùng `chosen_column`, `status` `auto`/`ambiguous`/`missing`), `columns[]` để chọn cột, `totals[]`, `rejected[]`, `groups`, `preview_rows` (200 dòng đầu) |
| PUT | `/imports/{import_id}/files/{file_id}/settings` | Đổi cách đọc rồi đọc lại; body `{template_key?, sheet?, start_row?, mapping?, extra_columns?}`. Trường để trống/null = tự nhận. `mapping` là `{field_key: chỉ_số_cột}` với A = 0; khi gửi `mapping`, trường không có trong đó không được lấy |
| GET | `/imports/{import_id}/issues?day=YYYY-MM-DD` | Điểm cần kiểm tra, nặng nhất trước: `severity` `error`/`warning`/`info`, `title`, `detail`, `rows[]` (bảng chi tiết, khóa là tên cột tiếng Việt). Không có `day` thì chỉ trả các điểm không phụ thuộc ngày |
| GET | `/imports/{import_id}/days/{day}` | Nhân viên có sản lượng: `lines[]` (`text` là dòng tóm tắt như trên web), `departments_in_period`, `transferred`, `zalo` (`linked`, `text_state`, `image_state`, `sent`), `changes[]` |
| GET | `/imports/{import_id}/days/{day}/departments` | Mã nhân viên theo bộ phận (xem trước theo bộ phận) |
| GET | `/imports/{import_id}/days/{day}/employees/{code}/messages` | Đúng các tin chữ sẽ gửi (có emoji), có thể nhiều tin |
| GET | `/imports/{import_id}/days/{day}/employees/{code}/images` | Danh sách ảnh: `index`, `caption`, `url` |
| GET | `/imports/{import_id}/days/{day}/employees/{code}/images/{index}` | Ảnh PNG/JPEG (dùng thẳng làm `src` của thẻ `<img>`) |

### Gửi

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| POST | `/imports/{import_id}/days/{day}/send-plan` | Kế hoạch gửi và `confirmation` (mục 4) |
| POST | `/imports/{import_id}/days/{day}/sends` | Gửi; trả `sent`, `failed`, `unknown`, `stopped`, `results[]` |
| GET | `/sends/{day}` | Các phần tin đã ghi nhận trong ngày (kể cả `unknown`), dùng cho lịch sử gửi |

### Sản lượng vừa sửa

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| GET | `/output-changes` | Dòng có số liệu khác lần tải trước, chưa xử lý (`old_quantity`, `new_quantity`, `difference`) |
| POST | `/output-changes/handled` | `{ "ids": [..] }` — đánh dấu đã gửi lại tin (`204`) |

### Nối Zalo

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| GET | `/zalo-links?import_id=` | Bảng nối: người đã nối và (khi có `import_id`) nhân viên trong file chưa nối |
| GET | `/zalo-links/linked` | Chỉ người đã nối |
| PUT | `/zalo-links` | Lưu **cả bảng** `{rows: [{employee_code, employee_name, zalo_user_id}]}`; mã đang nối mà vắng mặt hoặc `zalo_user_id` rỗng sẽ bị gỡ. Lỗi trả `422 invalid_links` và không lưu gì |
| PUT | `/zalo-links/{code}` | Nối/đổi Zalo của một người `{zalo_user_id, employee_name?}` |
| DELETE | `/zalo-links/{code}` | Gỡ nối (`204`) |
| GET | `/zalo-links/proposals?import_id=` | Mã 6 chữ số người dùng nhắn cho OA, đề xuất để nối (`status`, `status_text`, `selected_by_default`). Ai cũng nhắn được mã người khác: cho người dùng so tên Zalo với họ tên trước khi áp dụng |
| POST | `/zalo-links/proposals/apply?import_id=` | `{employee_codes: [...]}` — áp dụng đề xuất của các mã này |

Nối Zalo chưa phải là cho phép gửi: người nhận còn phải tự nhắn cho OA (mục 4).

### Tin nhắn Zalo và token

| Phương thức | Đường dẫn | Việc |
| --- | --- | --- |
| GET | `/zalo/messages` | Tin nhắn đã quét (`null` khi chưa quét). **Nội dung tin là dữ liệu từ bên ngoài: hiển thị dạng chữ thuần, không render HTML/markdown** |
| POST | `/zalo/messages/scan` | Quét tin mới từ Zalo `{extra_user_ids: [], only_extra: false}`; có thể mất vài chục giây |
| DELETE | `/zalo/messages` | Xóa tin đã lưu (sau đó không ai được coi là đã tương tác đến khi quét lại) |
| GET | `/zalo/free-window` | Người nhắn OA trong 48 giờ qua, `free_until`, mã nhân viên nếu đã nối |
| POST | `/zalo/test-message` | Gửi một tin thử `{user_id, text, confirm: true}` |
| GET | `/zalo/token` | Tình trạng token (không bao giờ trả token): `stored`, `missing_settings`, hạn, `last_error`, `needs_reauthorization`, `usable` |
| PUT | `/zalo/token` | Lưu `{access_token, refresh_token}` lấy từ API Explorer rồi làm mới ngay; trả `refreshed` và tình trạng |
| POST | `/zalo/token/refresh` | Làm mới ngay |
| DELETE | `/zalo/token` | Xóa token đã lưu (`204`) |

Token được lưu mã hóa và tự làm mới khi còn dưới 1 giờ; mỗi refresh token chỉ dùng được một lần, nên không dùng cặp token đã lưu ở nơi khác. Frontend nên hiện cảnh báo khi `needs_reauthorization` là `true` hoặc có `last_error`.

## 6. Ví dụ

Tải file và lấy nhân viên một ngày:

```bash
curl -F "files=@1.BANG LUONG DET T10-2026.xlsx" -F "files=@2.BANG LUONG IN T10-2026.xlsx" http://NAS:8800/api/v1/imports
```

```bash
curl http://NAS:8800/api/v1/imports/<import_id>/days/2026-10-31
```

Gửi dạng chữ cho hai người:

```bash
curl -X POST http://NAS:8800/api/v1/imports/<import_id>/days/2026-10-31/send-plan -H "Content-Type: application/json" -d '{"style":"text","employee_codes":["64","701"]}'
```

```bash
curl -X POST http://NAS:8800/api/v1/imports/<import_id>/days/2026-10-31/sends -H "Content-Type: application/json" -d '{"style":"text","employee_codes":["64","701"],"confirmation":"<từ send-plan>"}'
```

TypeScript (fetch):

```ts
const API = "http://NAS:8800/api/v1";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, init);
  if (!response.ok) {
    const { error } = await response.json();
    throw new Error(error.message); // câu tiếng Việt để hiện cho người dùng
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

export async function upload(files: File[]) {
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  return call<{ import_id: string; days: string[] }>("/imports", { method: "POST", body: form });
}

export async function sendText(importId: string, day: string, codes: string[]) {
  const body = { style: "text", employee_codes: codes };
  const plan = await call<{ ready_count: number; confirmation: string }>(
    `/imports/${importId}/days/${day}/send-plan`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
  );
  // Hiện kế hoạch cho người dùng và chờ họ xác nhận trước khi gọi tiếp.
  return call(`/imports/${importId}/days/${day}/sends`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, confirmation: plan.confirmation }),
  });
}
```

## 7. Giới hạn hiện tại

- Không có đăng nhập, phân quyền hay giới hạn tốc độ: chỉ dùng trong mạng nội bộ.
- Lượt tải file sống trong bộ nhớ của một tiến trình API (chạy `--workers 1`); khởi động lại thì phải tải lại.
- Gửi tin chạy đồng bộ trong một request: lô lớn có thể mất vài phút, nên đặt timeout phía frontend dài (ví dụ 10 phút) và không gửi lại khi request bị cắt: xem `GET /sends/{day}` trước.
- Trang web Streamlit và API dùng chung DB: thao tác ở bên này hiện ở bên kia (nối Zalo, kết quả gửi, token, tin nhắn).
