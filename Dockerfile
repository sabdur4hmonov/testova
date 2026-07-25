FROM python:3.11-slim
# poppler-utils: PDF page rendering.
# libreoffice-writer (pulls libreoffice-core): headless DOCX->PDF conversion
#   for Defect 3 — DOCX files whose diagrams/equations are VML/OMML shapes are
#   rendered to PDF so their figures can be cropped. Writer-only (not the full
#   suite) keeps the image growth to ~350-500 MB. `soffice` lands on PATH; the
#   non-root user gets a per-call writable profile via -env:UserInstallation
#   (see file_processor.docx_to_pdf), so no extra HOME setup is needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils wget libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser:appuser /app
USER appuser
RUN mkdir -p storage/uploads storage/processed storage/exports logs
CMD ["python", "main.py"]
