FROM python:3.12-slim
RUN pip install --no-cache-dir psycopg2-binary pika
WORKDIR /app
COPY worker.py .
CMD ["python", "worker.py"]
