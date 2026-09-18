FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi uvicorn pypdf openpyxl python-multipart psycopg2-binary pika pydantic
WORKDIR /app
COPY app.py .
EXPOSE 5000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000"]
