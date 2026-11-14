FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --create-home appuser && mkdir -p /srv/data && chown appuser /srv/data
USER appuser

EXPOSE 8000
# --proxy-headers: the visitor's address comes from X-Forwarded-For only when the request arrives from a proxy on one
# of the private networks in FORWARDED_ALLOW_IPS (Caddy, cloudflared), reading from the right, so a visitor can't
# fake it. WEB_CONCURRENCY sets the number of worker processes.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
