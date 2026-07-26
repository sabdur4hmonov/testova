FROM python:3.11-slim
# poppler-utils: PDF page rendering.
# libreoffice-writer + libreoffice-math (both pull libreoffice-core): headless
#   DOCX->PDF conversion for Defect 3 — DOCX files whose diagrams/equations are
#   VML/OMML shapes are rendered to PDF so their figures can be cropped.
#   libreoffice-math is REQUIRED, not optional: proven empirically, writer alone
#   renders VML drawings but leaves embedded OMML equations (OLE Math objects)
#   BLANK — the vertical-subtraction puzzle vanished until math was added.
#   Writer+math (not the full suite) keeps the image growth to ~350-500 MB.
#   `soffice` lands on PATH; the non-root user gets a per-call writable profile
#   via -env:UserInstallation (see file_processor.docx_to_pdf), no HOME setup.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils wget libreoffice-writer libreoffice-math \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser:appuser /app
USER appuser
RUN mkdir -p storage/uploads storage/processed storage/exports logs
CMD ["python", "main.py"]
