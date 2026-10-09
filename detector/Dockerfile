# Testova AI-text detector: FastAPI + onnxruntime (CPU) + Tesseract OCR.
# Build context is ./detector (see docker-compose.yml); model files are NOT in
# the image — they are mounted read-only at /models.
FROM python:3.11-slim

# Tesseract with the four languages the detector reads from photos:
# eng comes with tesseract-ocr; uzb, uzb_cyrl, rus are separate packs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-uzb tesseract-ocr-uzb-cyrl tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt detector/requirements.txt
RUN pip install --no-cache-dir -r detector/requirements.txt
COPY __init__.py detector/__init__.py
COPY service detector/service

RUN adduser --disabled-password --gecos '' appuser
USER appuser

ENV DETECTOR_MODELS_DIR=/models \
    DETECTOR_THREADS=4 \
    OMP_NUM_THREADS=4
EXPOSE 8010
CMD ["uvicorn", "detector.service.api:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
