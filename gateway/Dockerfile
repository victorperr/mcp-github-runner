FROM python:3.12-slim

WORKDIR /srv
# Exact versions with hashes (see requirements.txt for how to regenerate it).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app

# Run as an unprivileged user; it only needs to write the SQLite volume.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway \
    && mkdir -p /data && chown gateway /data

# SQLite lives on a volume so registered servers survive restarts.
ENV GATEWAY_DB_PATH=/data/gateway.db
VOLUME ["/data"]
USER gateway
EXPOSE 8000

# One worker: in-flight calls are held in memory (see README, "Scaling").
# --proxy-headers trusts X-Forwarded-For only from FORWARDED_ALLOW_IPS (default 127.0.0.1):
# set it to your load balancer's address so audit logs get real client IPs.
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
