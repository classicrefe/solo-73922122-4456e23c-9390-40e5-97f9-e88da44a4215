FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CATALOG_DB=/data/catalog.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker + 多线程：进程内重算锁可串行化所有写操作，SQLite 也最稳妥
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "300", "app.wsgi:app"]
