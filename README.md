# Gửi sản lượng

Ứng dụng gửi báo cáo sản lượng sản xuất qua Zalo Official Account. Quy tắc dự án nằm ở [AGENTS.md](src/gui_san_luong/AGENTS.md).

## Tổng hợp sản lượng theo nhân viên

```bash
uv run python -m streamlit run src/gui_san_luong/ui/app.py --server.address 127.0.0.1
```

Tải nhiều file Excel, mỗi file có một tab để chọn sheet và cột. Cuối trang chọn ngày để xem sản lượng từng nhân viên trên mọi file, gộp theo mã số nên người đổi công đoạn trong tháng vẫn là một dòng. Chưa kết nối Zalo nên chưa gửi được.

Tab “Nối Zalo” dùng để nhập Zalo user id cho từng mã nhân viên. Dữ liệu lưu ở file SQLite `data/gui_san_luong.db`, đổi được bằng biến môi trường `GUI_SAN_LUONG_DB`.

## Thử nghiệm với file Excel thật

Bỏ file vào [data/excel_samples](data/excel_samples/README.md). Các file này không được commit.
