FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils wget && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser:appuser /app
USER appuser
RUN mkdir -p storage/uploads storage/processed storage/exports logs
CMD ["python", "main.py"]
