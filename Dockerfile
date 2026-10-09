# Живая лента — образ для Docker и docker compose.
#
# Собран на python:3.12-slim: там есть и pip, и Pillow с готовыми библиотеками.
# Alpine не берём — musl собирает Pillow из исходников, сборка идёт минуты.

FROM python:3.12-slim

# Pillow тянет libjpeg и zlib, без них не работает обработка фото.
# tini нужен, чтобы SIGTERM доходил до uvicorn и он корректно гасил сокеты.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        tini \
    && rm -rf /var/lib/apt/lists/*

# Приложение не должно работать от root
RUN groupadd --system --gid 1000 livefeed \
    && useradd --system --uid 1000 --gid livefeed \
        --create-home --home-dir /home/livefeed --shell /usr/sbin/nologin livefeed

WORKDIR /app

# Сначала зависимости: правка кода не должна пересобирать pip
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY run.py ./

# Данные вынесены наружу через volume. Внутри data/ появятся папки событий,
# их создаёт само приложение при первом событии.
RUN mkdir -p /app/data && chown -R livefeed:livefeed /app
VOLUME ["/app/data"]

USER livefeed
EXPOSE 8000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LF_HOST=0.0.0.0 \
    LF_PORT=8000 \
    LF_DATA_DIR=/app/data

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# tini первым: он пересылает сигналы uvicorn, иначе контейнер
# не остановится по Ctrl+C и оставит WebSocket-соединения висеть
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
