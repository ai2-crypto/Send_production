# Chép mã nguồn cần để build trên NAS vào deploy\gui-san-luong, bỏ những gì ghi trong .nasignore.
# Chạy: powershell -ExecutionPolicy Bypass -File scripts\prepare_nas.ps1
# Rồi dùng FileZilla tải nội dung thư mục deploy\gui-san-luong lên thư mục dự án trên NAS.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $root "deploy\gui-san-luong"

$patterns = Get-Content -Encoding UTF8 (Join-Path $root ".nasignore") |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and -not $_.StartsWith("#") }
$excludedDirs = @($patterns | Where-Object { $_.EndsWith("/") } | ForEach-Object { $_.TrimEnd("/") })
$excludedFiles = @($patterns | Where-Object { -not $_.EndsWith("/") })

# Xóa bản cũ trước: robocopy không xóa ở đích những file vừa được thêm vào danh sách loại trừ.
# Chỉ xóa phần bên trong, vì thư mục có thể đang mở trong FileZilla hoặc Explorer.
if (Test-Path $destination) {
    Get-ChildItem -Force $destination | Remove-Item -Recurse -Force
}
$arguments = @($root, $destination, "/E", "/NJH", "/NJS", "/NDL", "/NP", "/XD") + $excludedDirs + @("/XF") + $excludedFiles
& robocopy @arguments | Out-Null
# Mã thoát của robocopy từ 8 trở lên mới là lỗi.
if ($LASTEXITCODE -ge 8) {
    throw "robocopy lỗi, mã $LASTEXITCODE"
}

# Thư mục data rỗng: Container Manager không tự tạo thư mục còn thiếu khi gắn volume
# ("Bind mount failed ... does not exist"). Rỗng nên tải lên không đè DB đang có trên NAS.
New-Item -ItemType Directory -Force (Join-Path $destination "data") | Out-Null

$files = Get-ChildItem -Recurse -File $destination
Write-Host "Đã chuẩn bị $($files.Count) file trong $destination"
$files | ForEach-Object { "  " + $_.FullName.Substring($destination.Length + 1) }
