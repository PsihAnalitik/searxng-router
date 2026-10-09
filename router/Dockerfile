FROM python:3.13-alpine@sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a
WORKDIR /app
COPY router/app.py /app/app.py
COPY router/http_client.py router/google_cse.py /app/router/
COPY router/packs.json /app/packs.json
COPY scripts/render-settings.py /app/scripts/render-settings.py
COPY core-config/settings.template.yml /app/core-config/settings.template.yml
COPY backend-config/settings.template.yml /app/backend-config/settings.template.yml
RUN adduser -D -u 1000 router
USER router
EXPOSE 8090
CMD ["python", "-u", "/app/app.py"]
