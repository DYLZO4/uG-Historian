FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY daq2.py ingest.py ./
# Unbuffered so stats lines show up in `docker compose logs` straight away
ENV PYTHONUNBUFFERED=1
CMD ["python", "ingest.py"]
