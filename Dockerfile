FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app
COPY app.py /app/app.py

RUN addgroup -S app && adduser -S -G app app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app
EXPOSE 8080
VOLUME ["/data"]
CMD ["python", "/app/app.py"]
