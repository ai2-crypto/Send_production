---
name: nas-docker-deploy
description: Quy trình đóng gói, tải lên, build image và chạy container của ứng dụng trên NAS Synology (DSM 7.3, Container Manager) tại /volume1/docker/gui-san-luong. Dùng skill này mỗi khi cần build hoặc cập nhật image, tạo hoặc chạy lại container, đổi cổng, sửa Dockerfile/compose.yaml/.nasignore/scripts/prepare_nas.ps1, hay gỡ lỗi container trên NAS, kể cả khi người dùng chỉ nói "đẩy lên NAS", "deploy", "build lại", "container không chạy", "Permission denied", "Bind mount failed", "seccomp" hoặc dán log từ NAS.
---

# Build và chạy container trên NAS Synology

Ứng dụng Streamlit chạy trong một container trên NAS Synology (DSM 7.3, Container Manager), thư mục dự án `/volume1/docker/gui-san-luong`, mở ở `http://<IP-NAS>:8900`. Hướng dẫn cho người dùng nằm ở mục "Chạy bằng Docker trên NAS Synology" trong `README.md`; khi đổi quy trình, sửa cả README và skill này.

Agent không có quyền vào NAS: mọi lệnh trên NAS do người dùng chạy qua SSH rồi dán kết quả. Đưa lệnh trong khối `bash`, mỗi lệnh một khối, và luôn kèm cách kiểm tra kết quả.

## Các file liên quan

| File | Vai trò |
| --- | --- |
| `Dockerfile` | Nhiều tầng, không dùng `RUN --mount` hay `# syntax=` (xem ràng buộc 1). Streamlit chạy cổng 8900, uid mặc định 1000, phông DejaVu cho ảnh màu |
| `compose.yaml` | Không có mục `build`, `pull_policy: never`, cổng `8900:8900`, `user: "${PUID:-1000}:${PGID:-1000}"`, gắn `./data` và `./.env:ro` |
| `.dockerignore` | Không cho `.env`, DB, `data`, `tests`, `.claude` vào image |
| `.nasignore` | Những gì không tải lên NAS (`.venv`, file AI, `.git`, `tests`, `data`, `.env`...) |
| `scripts/prepare_nas.ps1` | Tạo bản sạch trong `deploy\gui-san-luong` theo `.nasignore`, kèm thư mục `data` rỗng. File phải lưu UTF-8 **có BOM**, vì Windows PowerShell 5.1 đọc sai tiếng Việt khi không có BOM |

## Ràng buộc của Synology (đã gặp thật, đừng thử lại)

1. **Không dùng được BuildKit.** Nhân Linux của NAS không có seccomp, mọi bước `RUN` dưới BuildKit lỗi `seccomp: config provided but seccomp not supported` (dòng `# syntax=docker/dockerfile:1` cũng lỗi `failed to solve: exit code: 1`). Luôn build bằng trình build cũ `DOCKER_BUILDKIT=0`, nên Dockerfile không được dùng tính năng chỉ BuildKit có. Nút **Xây dựng** của Container Manager dùng BuildKit, nên compose không có mục `build`.
2. **`sudo` bỏ qua biến đặt trước nó.** Viết `sudo DOCKER_BUILDKIT=0 docker ...`, không viết `DOCKER_BUILDKIT=0 sudo docker ...`.
3. **ACL của DSM thắng quyền Unix.** Thư mục trong `/volume1/docker` có ACL (dấu `+` cuối chuỗi quyền, ví dụ `drwxrwxrwx+`), thường cấp quyền qua nhóm `administrators` mà tiến trình trong container không có. Kết quả: `Permission denied` / `sqlite3.OperationalError: unable to open database file` dù container đúng là chủ thư mục. Sửa bằng `synoacltool -del` cho riêng `data` và `.env`.
4. **Container Manager không tự tạo thư mục gắn vào.** Thiếu `data` thì báo `Bind mount failed: '.../data' does not exist`.
5. **`.env` phải là file có sẵn.** Thiếu thì Docker tạo một *thư mục* tên `.env`. Container không đọc được `.env` thì ứng dụng lặng lẽ coi như chưa có token, không báo lỗi.

## Cập nhật phiên bản (việc thường làm nhất)

1. Trên máy Windows, tại thư mục dự án, tạo bản sạch:

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts\prepare_nas.ps1
   ```

2. Người dùng dùng FileZilla (SFTP) kéo **nội dung** `deploy\gui-san-luong` đè lên `/volume1/docker/gui-san-luong`. Bản sạch không có `.env` và chỉ có `data` rỗng, nên token và DB trên NAS được giữ. FileZilla không xóa file cũ: file bị xóa khỏi dự án phải xóa tay trên NAS.
3. Build image trên NAS (lần đầu vài phút; dòng `DEPRECATED: The legacy builder...` là cảnh báo, không phải lỗi):

   ```bash
   cd /volume1/docker/gui-san-luong && sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .
   ```

4. Chạy lại container với image mới:

   ```bash
   cd /volume1/docker/gui-san-luong && sudo docker compose up -d --force-recreate
   ```

Chỉ đổi `compose.yaml` hoặc `.env` (cổng, PUID, token) thì bỏ bước 3. Đổi `Dockerfile`, mã nguồn, `pyproject.toml`/`uv.lock` hoặc `migrations` thì phải build lại.

## Cài lần đầu trên NAS

Trình tự đã chạy được trên NAS này (tài khoản `it` là `uid=1027 gid=100`). Thứ tự quan trọng: `PUID` đúng và không còn ACL trước khi chạy container. Bản cho người dùng, kèm giải thích, ở mục “Cài lần đầu trên NAS” của `README.md`.

1. Tải bản sạch lên (có sẵn `data` rỗng).
2. Tạo `.env` **trên NAS** (sửa trên Windows sẽ dính `\r`): `cp .env.example .env && sudo vi .env`, điền `ZALO_APP_ID`, `ZALO_SECRET_KEY`. Access token và refresh token không để trong `.env`: dán một lần ở tab “Tin nhắn Zalo (thử)”, app lưu mã hóa trong DB và tự làm mới.
3. `id it`, rồi ghi đúng một cặp PUID/PGID và kiểm tra compose ra `user: 1027:100`:

   ```bash
   cd /volume1/docker/gui-san-luong && sudo sed -i 's/\r$//; /^[[:space:]#]*P[UG]ID[[:space:]]*=/d' .env && printf 'PUID=1027\nPGID=100\n' | sudo tee -a .env >/dev/null && sudo docker compose config | grep user
   ```

4. Chủ và quyền (dùng `;`, không `&&`):

   ```bash
   cd /volume1/docker/gui-san-luong && sudo chown -R 1027:100 data .env; sudo chmod 755 data; sudo chmod 600 .env; ls -lnd data .env
   ```

   Còn dấu `+` thì gỡ ACL từng cái (`sudo synoacltool -del <đường dẫn đầy đủ>`).
5. Build (`sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .`), tạo khóa `sudo docker run --rm gui-san-luong:latest python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` và chép vào `TOKEN_ENCRYPTION_KEY=`.
6. `sudo docker compose up -d` (hoặc **Dự án** trong Container Manager, không bấm **Xây dựng**).
7. Kiểm tra (mục dưới), rồi dán token trên trang và bấm “Lưu và làm mới ngay”.

## Kiểm tra

Container chạy đúng tài khoản, ghi được `data`, đọc được `.env` (phải in `uid=1027`, `GHI_DUOC`, `DOC_DUOC_ENV`):

```bash
sudo docker exec gui-san-luong sh -c 'id; touch /app/data/_t && echo GHI_DUOC && rm /app/data/_t; test -r /app/.env && echo DOC_DUOC_ENV'
```

Trạng thái (`healthy` sau khoảng 30 giây) và log (lỗi gốc SQLite/Zalo ghi ở đây, giao diện chỉ hiện câu ngắn):

```bash
sudo docker ps --filter name=gui-san-luong
```

```bash
sudo docker logs --tail 50 gui-san-luong
```

Lệnh chẩn đoán gộp, nhờ người dùng dán toàn bộ kết quả khi chưa rõ lỗi (không in token hay secret):

```bash
cd /volume1/docker/gui-san-luong && ls -lnd . data .env; sudo grep -n 'P[UG]ID' .env | cat -A; sudo docker compose config | grep user; sudo docker exec gui-san-luong sh -c 'id; touch /app/data/_t && echo GHI_DUOC && rm /app/data/_t; test -r /app/.env && echo DOC_DUOC_ENV'; sudo docker logs --tail 30 gui-san-luong
```

Không bao giờ nhờ người dùng in các dòng `ZALO_*`, `TOKEN_ENCRYPTION_KEY` hay token ra cuộc trò chuyện. Khi cần biết dòng đó có ký tự lạ, chỉ hỏi kết quả có `^M` hay không.

## Bảng lỗi

Mọi dòng dưới đây đã gặp thật khi triển khai trên NAS này (2026-11-19 và 2026-11-20), trừ dòng `RUN --mount`. Hướng dẫn đầy đủ cho người dùng nằm ở mục “Khắc phục sự cố” của `README.md`; khi thêm lỗi mới, thêm vào cả hai nơi.

| Triệu chứng | Nguyên nhân | Cách sửa |
| --- | --- | --- |
| `failed to solve: exit code: 1` ngay sau `resolve image config for docker.io/docker/dockerfile:1` | Dòng `# syntax=` buộc dùng bộ đọc BuildKit mới | Không thêm `# syntax=` vào Dockerfile; tải lại `Dockerfile` mới |
| `runc run failed: ... seccomp not supported` | Build bằng BuildKit (thiếu `DOCKER_BUILDKIT=0`, biến đặt trước `sudo`, hoặc bấm **Xây dựng**) | `sudo DOCKER_BUILDKIT=0 docker build ...` |
| `the --mount option requires BuildKit` | Dockerfile bị thêm `RUN --mount` | Bỏ `RUN --mount`, cài thư viện theo kiểu `COPY pyproject.toml uv.lock` rồi `RUN uv sync` |
| `Bind mount failed: '.../data' does not exist` | Chưa có thư mục `data` | `mkdir -p data`, rồi chủ/quyền như bước 4 |
| `.env` là thư mục | Chạy container khi chưa có file `.env` | `sudo rmdir .env`, tạo lại file như bước 2 |
| Trang báo `Không đọc hoặc ghi được lịch sử thay đổi sản lượng`; log `unable to open database file`; `touch` báo `Permission denied` | Container không ghi được `data`: uid trong container khác chủ thư mục, hoặc còn ACL (`+`) | So `id` với `ls -lnd`; gỡ ACL; `chown` theo `PUID`; `docker restart gui-san-luong` |
| `synoacltool -del` báo `(synoacltool.c, 672)Unknown error` | Thường là không còn ACL để gỡ | Xem `ls -lnd data .env`: không còn `+` thì bỏ qua; không nối lệnh này bằng `&&` vì lỗi chặn các lệnh sau |
| Container vẫn chạy `uid=1000` (hoặc chỉ `gid` đổi) dù `.env` có `PUID`/`PGID` | Dòng `PUID` trống, trùng (dòng trống phía dưới ghi đè, ví dụ sau khi chép lại `.env.example`), có `#`, hoặc dính `\r` do sửa `.env` trên Windows | Lệnh ở bước 3, `chown 1027:100 .env`, rồi `docker rm -f` và `compose up -d` (đổi tài khoản phải tạo lại container, `restart` không đủ) |
| Hỏng lại quyền ngay sau khi đã chạy đúng | Người dùng chạy thêm `chown ... 1000` (cách dự phòng) hoặc `chmod 1027` (nhầm với `chown`) | `chown -R 1027:100 data .env`, `chmod 755 data`, `chmod 600 .env`; chủ luôn phải trùng `PUID` |
| `compose up` báo trùng tên container | Container cũ tạo bằng cách khác | `sudo docker rm -f gui-san-luong` rồi chạy lại |
| Không thấy ô “Refresh token” | NAS chạy image cũ | Cập nhật phiên bản; kiểm tra `sudo docker exec gui-san-luong python -c "import gui_san_luong.services.zalo_token; print('ban moi')"` |
| Nút “Lưu và làm mới ngay” bị khóa | `.env` thiếu `ZALO_APP_ID`/`ZALO_SECRET_KEY`/`TOKEN_ENCRYPTION_KEY` (dòng trên nút ghi biến nào), hoặc container không đọc được `.env` | Điền rồi tải lại trang; kiểm tra ra `DOC_DUOC_ENV`; dòng dính `\r` thì chạy `sed -i 's/\r$//' .env` |
| `ModuleNotFoundError: No module named 'cryptography'` khi tạo khóa | Chạy bằng `python` thường | Dùng `docker run --rm gui-san-luong:latest python ...` trên NAS, `uv run python ...` trên máy |
| Zalo báo `-14004: Invalid secret key` | `ZALO_SECRET_KEY` không khớp `ZALO_APP_ID` (thường nhầm với OA Secret Key của Webhook) | Sửa `.env`, tải lại trang, bấm “Làm mới ngay”; nếu sau đó báo `-14014` thì lần lỗi trước đã tiêu mất refresh token (đã gặp 2026-11-21): lấy cặp mới |
| Trang báo Zalo từ chối refresh token (`-14014`) trong khi gửi/đọc tin vẫn được | Access token dán vẫn còn hạn (tối đa 25 giờ) nhưng refresh token đã bị dùng: lần làm mới lỗi trước, nơi khác, hoặc bấm “Lấy Access Token” thêm lần nữa trên API Explorer | Bấm “Lấy Access Token” một lần, dán ngay cặp mới, “Lưu và làm mới ngay”; sau đó không lấy token trên API Explorer nữa |
| Không giải mã được token đã lưu | `TOKEN_ENCRYPTION_KEY` đã đổi | Đặt lại khóa cũ, hoặc dán lại token |
| Không mở được trang | Container chưa `healthy`, hoặc tường lửa DSM chặn 8900 | `docker ps`, log; mở 8900 cho mạng nội bộ, không mở ra Internet |
| Cổng hiển thị khác 8900 | `compose.yaml` hoặc image cũ | Cổng NAS là số bên trái trong `"8900:8900"` (chỉ cần `docker compose up -d`); cổng trong container đặt ở `Dockerfile` (đổi phải build lại, sửa cả `EXPOSE` và healthcheck) |

## Khi sửa file Docker trên máy phát triển

- Kiểm tra bằng đúng trình build của NAS, từ bản sạch, rồi dọn image thử:

  ```bash
  powershell -ExecutionPolicy Bypass -File scripts\prepare_nas.ps1
  ```

  ```bash
  cd deploy/gui-san-luong && DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t gui-san-luong:test .
  ```

  Chạy thử bằng một bản sao `compose.yaml` với `.env` rỗng và thư mục `data` tạm (không dùng `.env` thật), chờ `healthy`, gọi `http://127.0.0.1:8900/_stcore/health`, rồi `docker compose down` và `docker rmi`.
- Docker Desktop trên Windows không tái hiện được lỗi ACL hay seccomp; muốn tái hiện lỗi quyền, chạy container bằng `-u 1026:100` trên một volume thuộc uid khác.
- Trang chưa có đăng nhập: không mở cổng ra Internet (port-forward, QuickConnect, reverse proxy công khai). Không đưa `.env`, DB hay file Excel thật vào image hoặc bản sạch.
