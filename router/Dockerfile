# Зависимостей нет намеренно: pip install при сборке рвётся вместе с сетью,
# поэтому app.py написан на stdlib, включая минимальный клиент valkey.
FROM python:3.13-alpine

WORKDIR /app
COPY app.py /app/app.py

RUN adduser -D -u 1000 router
USER router

EXPOSE 8090
CMD ["python", "-u", "/app/app.py"]
