FROM python:3.12-slim

WORKDIR /srv
COPY pyproject.toml .
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "mcp>=1.20,<2" "httpx>=0.27" "pyjwt[crypto]>=2.8"
COPY app ./app

# SQLite lives on a volume so registered servers survive restarts.
ENV GATEWAY_DB_PATH=/data/gateway.db
VOLUME ["/data"]
EXPOSE 8000

# One worker: in-flight calls are held in memory (see README, "Scaling").
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
