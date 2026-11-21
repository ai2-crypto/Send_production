# Build bằng trình build cũ của Docker trên NAS Synology:
#   sudo DOCKER_BUILDKIT=0 docker build -t gui-san-luong:latest .
# Không dùng BuildKit: nhân Linux của Synology không có seccomp, nên mọi bước RUN dưới BuildKit
# lỗi "seccomp: config provided but seccomp not supported". Vì vậy file này không dùng
# RUN --mount hay dòng "# syntax=" (chỉ BuildKit hiểu). Vẫn build được bằng BuildKit trên máy khác.
# Hướng dẫn đầy đủ: mục "Chạy bằng Docker trên NAS Synology" trong README.md.

ARG PYTHON_IMAGE=python:3.14-slim-trixie

FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Thư viện cài trước mã nguồn, để sửa code không phải tải lại thư viện.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
# Cài dạng editable: config.py tìm data/, migrations/ và .env theo vị trí mã nguồn /app/src.
RUN uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE}
# DejaVu có đủ dấu tiếng Việt, dùng để vẽ ảnh sản lượng thay cho Segoe UI của Windows.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --user-group --no-create-home app
WORKDIR /app
COPY --from=build /app /app
COPY migrations ./migrations
COPY .streamlit/config.toml ./.streamlit/config.toml
RUN mkdir -p data && chown app:app data

ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Ho_Chi_Minh \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8900 \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

USER app
EXPOSE 8900 8800
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8900/_stcore/health', timeout=4)"]
CMD ["python", "-m", "streamlit", "run", "src/gui_san_luong/ui/app.py"]
