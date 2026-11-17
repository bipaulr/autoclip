FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# ffmpeg for render, pg_dump for nightly backups, open-licence fonts for captions
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg postgresql-client curl fonts-dejavu-core fonts-liberation fontconfig libgl1 libglib2.0-0 \
    && mkdir -p /usr/share/fonts/truetype/ofl \
    && curl -fsSL -o /usr/share/fonts/truetype/ofl/Anton-Regular.ttf \
        https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf \
    && curl -fsSL -o /usr/share/fonts/truetype/ofl/ArchivoBlack-Regular.ttf \
        https://github.com/google/fonts/raw/main/ofl/archivoblack/ArchivoBlack-Regular.ttf \
    && fc-cache -f && rm -rf /var/lib/apt/lists/*
# YuNet face detector model for face-tracked crops (OpenCV Zoo, Apache-2.0)
RUN mkdir -p /app/models && curl -fsSL -o /app/models/face_detection_yunet_2023mar.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
# yt-dlp needs a JavaScript runtime for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Headless Chromium for auto-submitting campaign links (integrations/cr_submit.py)
RUN python -m playwright install --with-deps chromium
# CUDA 12 maths libraries for GPU transcription (faster-whisper / CTranslate2). Unused on CPU-only machines.
RUN pip install --no-cache-dir "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

COPY alembic.ini .
COPY migrations ./migrations
COPY config ./config
COPY autoclip ./autoclip

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fs http://127.0.0.1:8000/health || exit 1
CMD ["uvicorn", "autoclip.main:app", "--host", "0.0.0.0", "--port", "8000"]
