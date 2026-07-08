FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.6 \
    uvicorn[standard]==0.32.1 \
    requests==2.32.3 \
    psycopg2-binary==2.9.10 \
    python-multipart==0.0.12 \
    openai==1.97.0

COPY main.py .

RUN useradd --uid 1000 --no-create-home --shell /sbin/nologin appuser
USER 1000

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
