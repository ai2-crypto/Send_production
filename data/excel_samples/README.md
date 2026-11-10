# File Excel sản lượng để thử nghiệm

Bỏ file Excel thật vào đây để AI agent đọc sản lượng và chạy thử. **Mọi thứ trong thư mục này đều bị Git bỏ qua, trừ tệp README này.** Thư mục nằm ngoài `src/` để dữ liệu không bị đóng gói cùng ứng dụng.

## Cách dùng

1. Chép file vào thư mục này, nên giữ nguyên bản gốc. Hỗ trợ `.xlsx` và `.xlsm`; file `.xls` cần lưu lại thành `.xlsx` trước.
2. Lưu và đóng file trong Excel để công thức có giá trị đã tính.
3. Nhờ agent, ví dụ: “Lấy sản lượng từ các file trong `data/excel_samples`”. Agent sẽ dùng skill `production-excel-import`.
4. Để kiểm tra độ chính xác, cho agent biết số đúng của ít nhất một file (ví dụ tổng sản lượng theo ngày). Agent lưu vào `expected/<tên-file>.json`, cũng không bị commit.

```text
data/excel_samples/
├── README.md
├── san_luong_2026-11-05.xlsx     # Ví dụ tên file
└── expected/
    └── san_luong_2026-11-05.json # Số đúng do người dùng xác nhận
```

Dữ liệu ở đây không được gửi qua Zalo khi thử nghiệm. Git không giữ bản sao các file này, nên file đã xóa không khôi phục lại được từ Git.
