# Gửi sản lượng

Ứng dụng gửi báo cáo sản lượng sản xuất qua Zalo Official Account. Quy tắc dự án nằm ở [AGENTS.md](src/gui_san_luong/AGENTS.md).

## Tổng hợp sản lượng theo nhân viên

```bash
uv run python -m streamlit run src/gui_san_luong/ui/app.py --server.address 127.0.0.1
```

Tải nhiều file Excel, mỗi file có một tab để chọn sheet và cột. Cuối trang chọn ngày để xem sản lượng từng nhân viên trên mọi file, gộp theo mã số nên người đổi công đoạn trong tháng vẫn là một dòng, rồi gửi cho từng người qua Zalo.

Tab “Nối Zalo” dùng để nhập Zalo user id cho từng mã nhân viên. Dữ liệu lưu ở file SQLite `data/gui_san_luong.db`, đổi được bằng biến môi trường `GUI_SAN_LUONG_DB`.

## API cho frontend khác

Mọi chức năng của trang web cũng có qua API HTTP/JSON (FastAPI), để tích hợp vào một frontend khác. Đặc tả và hướng dẫn tích hợp: [docs/API.md](docs/API.md); đặc tả OpenAPI: [docs/openapi.json](docs/openapi.json). Khi API chạy, tài liệu tương tác ở `/api/v1/docs`.

```bash
uv run python -m uvicorn gui_san_luong.api.app:app --host 127.0.0.1 --port 8800
```

API chưa có đăng nhập (người dùng chọn 2026-11-21): ai vào được cổng 8800 đều gửi được tin Zalo, nên chỉ dùng trong mạng nội bộ. Origin của frontend đặt ở `API_CORS_ORIGINS` trong `.env`. Trang web và API dùng chung DB, nên thao tác ở bên này hiện ở bên kia.

## Chạy bằng Docker trên NAS Synology (DSM 7.3)

Image gồm [Dockerfile](Dockerfile) (nhiều tầng, chạy bằng tài khoản không phải root) và [compose.yaml](compose.yaml). Compose chạy hai container từ cùng một image, dùng chung `data` và `.env`: `gui-san-luong` (trang web, cổng 8900) và `gui-san-luong-api` (API, cổng 8800). File `.env`, DB và file Excel thật không bao giờ được đóng vào image (xem [.dockerignore](.dockerignore)).

> Trang và API chưa có đăng nhập. Chỉ mở cổng 8900 và 8800 trong mạng nội bộ: không port-forward trên router, không gắn vào reverse proxy hay QuickConnect công khai.

Image được build ngay trên NAS qua SSH, nên không cần chép file image nào. **Trên Synology phải build bằng trình build cũ (`DOCKER_BUILDKIT=0`), không dùng được BuildKit:** nhân Linux của NAS không có seccomp, nên mọi bước `RUN` dưới BuildKit đều lỗi `seccomp: config provided but seccomp not supported`. Cũng vì vậy `compose.yaml` không có mục `build`, để Container Manager không tự build bằng BuildKit.

### Tải mã nguồn lên NAS bằng FileZilla

FileZilla không đọc được file ignore, nên dự án có một danh sách loại trừ ([.nasignore](.nasignore)) và một script ([scripts/prepare_nas.ps1](scripts/prepare_nas.ps1)) tạo bản sạch để tải lên. Mỗi lần đẩy code:

1. Tại thư mục dự án trên máy, chạy script để tạo bản sạch trong `deploy\gui-san-luong`:

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts\prepare_nas.ps1
   ```

2. Trong FileZilla (kết nối SFTP tới NAS), kéo **nội dung** của `deploy\gui-san-luong` vào thư mục dự án trên NAS, ví dụ `/volume1/docker/gui-san-luong`, chọn ghi đè khi được hỏi.
3. Build lại image qua SSH và chạy lại container (xem mục **Cập nhật phiên bản**; lần đầu xem **Cài lần đầu trên NAS**).

Những gì không được đẩy lên (sửa `.nasignore` để thêm hoặc bớt):

| Bỏ qua | Lý do |
| --- | --- |
| `.venv`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache` | Image tự cài thư viện khi build |
| `.claude/`, `AGENTS.md`, `CLAUDE.md` | File dành cho AI, không cần để chạy |
| `.git`, `.gitignore`, `scripts/`, `.nasignore`, `deploy/` | Chỉ dùng trên máy phát triển |
| `tests/`, `data/`, `*.db` | Test, file Excel và DB thật không cần để build |
| `.env`, `secrets.toml` | Tạo `.env` trực tiếp trên NAS một lần, để lần đẩy sau không ghi đè token trên NAS |

Lưu ý:

- `.env` và `data` trên NAS không bị đụng tới, vì bản sạch không có hai thứ này.
- FileZilla chỉ ghi đè chứ không xóa: file nào bị xóa khỏi dự án thì cần xóa tay trên NAS.
- `deploy/` đã có trong `.gitignore` nên bản sạch không bị commit.

### Cài lần đầu trên NAS

Làm qua SSH (bật ở **Bảng điều khiển → Terminal & SNMP**), trong thư mục dự án. Ví dụ dưới dùng tài khoản `it` có `uid=1027 gid=100`; thay bằng kết quả `id` của bạn.

1. Tải bản sạch lên như mục trên. Bản sạch có sẵn thư mục `data` rỗng.
2. Tạo `.env` từ mẫu. Sửa `.env` ngay trên NAS bằng `vi`: file sửa trên Windows rồi tải lên mang ký tự xuống dòng `\r`, làm hỏng các dòng như `PUID` (xem phần khắc phục).

   ```bash
   cd /volume1/docker/gui-san-luong && cp .env.example .env && sudo vi .env
   ```

   Điền `ZALO_APP_ID` và `ZALO_SECRET_KEY` (trang quản lý ứng dụng trên Zalo for Developers). Để `TOKEN_ENCRYPTION_KEY` đến bước 5. Access token và refresh token **không** điền vào `.env`.

3. Cho container chạy bằng tài khoản sở hữu thư mục. Xem UID/GID:

   ```bash
   id it
   ```

   Ghi đúng một cặp `PUID`/`PGID` vào `.env` (lệnh xóa mọi dòng PUID/PGID cũ trước, kể cả dòng trống chép từ `.env.example`), rồi kiểm tra compose nhận đúng; phải ra `user: 1027:100`:

   ```bash
   cd /volume1/docker/gui-san-luong && sudo sed -i 's/\r$//; /^[[:space:]#]*P[UG]ID[[:space:]]*=/d' .env && printf 'PUID=1027\nPGID=100\n' | sudo tee -a .env >/dev/null && sudo docker compose config | grep user
   ```

4. Đặt chủ và quyền cho `data` và `.env`, rồi xem kết quả:

   ```bash
   cd /volume1/docker/gui-san-luong && sudo chown -R 1027:100 data .env; sudo chmod 755 data; sudo chmod 600 .env; ls -lnd data .env
   ```

   Hai dòng phải có chủ `1027 100` và **không** có dấu `+` cuối chuỗi quyền. Còn dấu `+` thì gỡ ACL (xem phần khắc phục “Permission denied”).

5. Build image (lần đầu mất vài phút; dòng `DEPRECATED: The legacy builder is deprecated` là cảnh báo, không phải lỗi):

   ```bash
   cd /volume1/docker/gui-san-luong && sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .
   ```

   Tạo khóa mã hóa token bằng image vừa build, chép chuỗi in ra vào `TOKEN_ENCRYPTION_KEY=` trong `.env` (`sudo vi .env`). Giữ nguyên khóa này về sau: đổi khóa thì phải dán lại token.

   ```bash
   sudo docker run --rm gui-san-luong:latest python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

6. Chạy container. Có thể dùng **Container Manager → Dự án → Tạo** trỏ tới thư mục này, nhưng không bấm **Xây dựng** (nút đó dùng BuildKit):

   ```bash
   cd /volume1/docker/gui-san-luong && sudo docker compose up -d
   ```

7. Kiểm tra nhanh (mục bên dưới), rồi mở `http://<IP-NAS>:8900`, vào tab **“Tin nhắn Zalo (thử)”**, dán access token và refresh token lấy từ API Explorer, bấm **“Lưu và làm mới ngay”**. Thấy “Đã làm mới access token” là App ID và Secret key đúng. Sau đó không dùng refresh token đó ở nơi khác (Postman, máy khác): mỗi refresh token chỉ dùng được một lần.

### Cập nhật phiên bản

1. Trên máy: `powershell -ExecutionPolicy Bypass -File scripts\prepare_nas.ps1`, rồi tải nội dung `deploy\gui-san-luong` lên bằng FileZilla. `.env` và DB trong `data` không bị ghi đè.
2. Trên NAS, build lại và chạy lại container với image mới:

   ```bash
   cd /volume1/docker/gui-san-luong && sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .
   ```

   ```bash
   cd /volume1/docker/gui-san-luong && sudo docker compose up -d --force-recreate
   ```

Lệnh trên chạy lại cả hai container (trang web và API). Chỉ sửa `compose.yaml` hoặc `.env` thì không cần build lại. Đổi cổng: sửa số bên trái trong `"8900:8900"` của `compose.yaml` rồi `docker compose up -d` (Streamlit trong container chạy ở 8900, đặt trong `Dockerfile`). Đổi token Zalo hay `ZALO_*` trong `.env` chỉ cần tải lại trang: ứng dụng đọc lại `.env` mỗi lần dùng.

Ghi chú: DB SQLite nằm trong `data/` trên NAS nên giữ nguyên khi cập nhật. Token Zalo được trang tự làm mới; chỉ phải dán lại khi trang báo refresh token bị từ chối hoặc sắp hết hạn (3 tháng không dùng). Ảnh màu dùng phông DejaVu thay cho Segoe UI của Windows nên chữ hơi khác.

### Kiểm tra nhanh

Container chạy đúng tài khoản, ghi được `data`, đọc được `.env` (phải ra `uid=1027`, `GHI_DUOC`, `DOC_DUOC_ENV`):

```bash
sudo docker exec gui-san-luong sh -c 'id; touch /app/data/_t && echo GHI_DUOC && rm /app/data/_t; test -r /app/.env && echo DOC_DUOC_ENV'
```

API: `curl http://127.0.0.1:8800/api/v1/health` trên NAS phải ra `{"status":"ok"}`; log xem bằng `sudo docker logs --tail 50 gui-san-luong-api`.

Trạng thái (STATUS thành `healthy` sau khoảng 30 giây) và log (lỗi gốc của SQLite và Zalo ghi ở đây, trang chỉ hiện câu ngắn):

```bash
sudo docker ps --filter name=gui-san-luong
```

```bash
sudo docker logs --tail 50 gui-san-luong
```

Khi chưa rõ lỗi, chạy lệnh chẩn đoán gộp và gửi toàn bộ kết quả (lệnh không in token hay secret):

```bash
cd /volume1/docker/gui-san-luong && ls -lnd . data .env; sudo grep -n 'P[UG]ID' .env | cat -A; sudo docker compose config | grep user; sudo docker exec gui-san-luong sh -c 'id; touch /app/data/_t && echo GHI_DUOC && rm /app/data/_t; test -r /app/.env && echo DOC_DUOC_ENV'; sudo docker logs --tail 30 gui-san-luong
```

### Khắc phục sự cố

Các lỗi dưới đây đều đã gặp khi triển khai trên NAS này.

#### Build lỗi `seccomp: config provided but seccomp not supported`

Nhân Linux của Synology không có seccomp, nên BuildKit không chạy được bước `RUN` nào. Luôn build bằng trình build cũ, và đặt biến **sau** `sudo` (`sudo` bỏ qua biến đặt trước nó, nên `DOCKER_BUILDKIT=0 sudo docker ...` vẫn dùng BuildKit):

```bash
cd /volume1/docker/gui-san-luong && sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .
```

Không bấm **Xây dựng** trong Container Manager. `compose.yaml` cố ý không có mục `build` vì lý do này.

#### Build lỗi `failed to solve: exit code: 1` ngay sau `docker.io/docker/dockerfile:1`

`Dockerfile` trên NAS là bản cũ có dòng `# syntax=docker/dockerfile:1` (chỉ BuildKit dùng). Tải lại `Dockerfile` mới từ bản sạch rồi build bằng lệnh ở trên. Dockerfile hiện tại không có dòng `# syntax=` và không dùng `RUN --mount`.

#### Container Manager báo `Bind mount failed: '.../data' does not exist`

Thiếu thư mục `data` (Container Manager không tự tạo). Tạo rồi làm lại bước 4 của phần cài:

```bash
cd /volume1/docker/gui-san-luong && sudo mkdir -p data && sudo chown 1027:100 data && sudo chmod 755 data
```

Tương tự, nếu chưa có file `.env` khi chạy container, Docker tạo ra một **thư mục** tên `.env`. Xóa nó (`sudo rmdir .env`) rồi tạo lại file như bước 2.

#### Trang báo `Không đọc hoặc ghi được lịch sử thay đổi sản lượng`, log ghi `unable to open database file`, `touch` báo `Permission denied`

Container không ghi được `data`. Chạy lệnh chẩn đoán và so ba thứ:

- `id` trong container phải trùng chủ của `data` và `.env` trong `ls -lnd`.
- Chuỗi quyền không được có dấu `+`. Dấu `+` là ACL của DSM: ACL thắng quyền Unix và thường cấp quyền qua nhóm `administrators`, nhóm mà tiến trình trong container không có, nên vẫn bị chặn dù đúng chủ thư mục. Gỡ ACL **từng cái một** (không nối bằng `&&`):

  ```bash
  sudo synoacltool -del /volume1/docker/gui-san-luong/data
  ```

  ```bash
  sudo synoacltool -del /volume1/docker/gui-san-luong/.env
  ```

  Báo `(synoacltool.c, 672)Unknown error` thường là không còn ACL để gỡ: xem lại `ls -lnd data .env`, không còn `+` thì bỏ qua.
- Đặt lại chủ bằng **`chown`** theo đúng `PUID`/`PGID` (bước 4). Đừng nhầm với `chmod 1027` (đó là đặt quyền, sẽ làm hỏng quyền thư mục), và đừng `chown` sang 1000 khi container đang chạy bằng 1027.

Rồi `sudo docker restart gui-san-luong` và chạy lại phần kiểm tra nhanh.

#### Container vẫn chạy `uid=1000` (hoặc chỉ `gid` đổi) dù `.env` có `PUID`/`PGID`

Docker compose không đọc được `PUID`: dòng đó trống, bị một dòng `PUID=` trống phía dưới ghi đè (thường sau khi chép lại `.env.example`), có dấu `#`, hoặc dính ký tự `\r` do sửa `.env` trên Windows (`cat -A` hiện `^M$`). Chạy lại lệnh ở bước 3 của phần cài (xóa mọi dòng PUID/PGID rồi ghi lại), đặt lại chủ `.env`, rồi **tạo lại** container (đổi tài khoản chạy phải tạo container mới, `restart` không đủ):

```bash
cd /volume1/docker/gui-san-luong && sudo chown 1027:100 .env && sudo chmod 600 .env && sudo docker rm -f gui-san-luong; sudo docker compose up -d
```

#### `docker compose up` báo trùng tên container

Container cũ được tạo bằng cách khác (Container Manager, lần chạy trước). Xóa rồi chạy lại: `sudo docker rm -f gui-san-luong`, sau đó `sudo docker compose up -d`.

#### Không thấy ô “Refresh token” trên trang

NAS đang chạy image cũ. Cập nhật phiên bản (tải bản sạch, build lại, `--force-recreate`). Kiểm tra image mới đã chạy (in `ban moi`):

```bash
sudo docker exec gui-san-luong python -c "import gui_san_luong.services.zalo_token; print('ban moi')"
```

#### Nút “Lưu và làm mới ngay” bị khóa

`.env` thiếu `ZALO_APP_ID`, `ZALO_SECRET_KEY` hoặc `TOKEN_ENCRYPTION_KEY`; dòng ngay trên nút ghi biến nào thiếu. Điền vào `.env` rồi tải lại trang (F5). Đã điền mà trang vẫn báo thiếu: container không đọc được `.env` (kiểm tra nhanh phải ra `DOC_DUOC_ENV`; xem mục “Permission denied”), hoặc dòng đó dính `\r`/khoảng trắng (`sudo grep -n ZALO_ .env | cat -A` chỉ để xem có `^M` không; đừng gửi kết quả lệnh này cho ai vì nó in secret).

#### `ModuleNotFoundError: No module named 'cryptography'` khi tạo khóa

Lệnh tạo khóa phải chạy bằng Python có thư viện của dự án: trên NAS dùng `sudo docker run --rm gui-san-luong:latest python -c ...` (bước 5), trên máy tính dùng `uv run python -c ...`. Không chạy bằng `python` thường.

#### Zalo báo `-14004: Invalid secret key`

`ZALO_SECRET_KEY` không khớp với `ZALO_APP_ID`. Lấy đúng **Khóa bí mật của ứng dụng** (không phải OA Secret Key của Webhook) của cùng ứng dụng đã chọn trên API Explorer, viết liền không dấu nháy hay khoảng trắng. Sửa `.env`, tải lại trang, bấm **“Làm mới ngay”**. Nếu sau đó trang báo Zalo từ chối refresh token (`-14014`), lần làm mới lỗi trước đã tiêu mất refresh token (đã gặp): lấy cặp token mới như mục dưới.

#### Trang báo Zalo từ chối refresh token (`-14014: Invalid refresh token`)

Access token đang dùng vẫn chạy (gửi, đọc tin vẫn được) đến khi hết hạn, tối đa 25 giờ kể từ lúc lấy ở API Explorer, nên trang vẫn dùng được nhưng cảnh báo là đúng: sau đó sẽ không tự làm mới được. Refresh token chỉ dùng được một lần và đã bị dùng: bởi một lần làm mới lỗi trước đó (ví dụ khi còn sai Secret key), bởi một nơi khác (Postman, máy khác), hoặc vì đã bấm “Lấy Access Token” thêm lần nữa trên API Explorer (mỗi lần bấm làm cặp trước mất hiệu lực). Cũng có thể đã hết hạn 3 tháng.

Cách sửa: trên API Explorer bấm **“Lấy Access Token” một lần**, dán **ngay** cả hai token vào tab “Tin nhắn Zalo (thử)”, bấm **“Lưu và làm mới ngay”**. Thấy “Đã làm mới access token” là xong; sau đó không bấm lấy token trên API Explorer nữa.

#### Trang báo không giải mã được token đã lưu

`TOKEN_ENCRYPTION_KEY` đã bị đổi. Đặt lại khóa cũ, hoặc giữ khóa mới và dán lại token.

#### Không mở được `http://<IP-NAS>:8900`

Xem `sudo docker ps --filter name=gui-san-luong` (cả hai container phải `Up ... (healthy)`) và log. Container chạy mà vẫn không vào được: cho phép cổng 8900 (và 8800 cho API) trong mạng nội bộ ở **Bảng điều khiển → Bảo mật → Tường lửa**. Không mở cổng này ra Internet.

## Thử nghiệm với file Excel thật

Bỏ file vào [data/excel_samples](data/excel_samples/README.md). Các file này không được commit.
