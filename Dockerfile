# ForgePanel in a container.
#
#   docker build -t forgepanel .
#   docker run -d --name forgepanel -p 8084:8084 \
#     -v forgepanel-data:/app/database \
#     -v forgepanel-custom:/app/custom_services \
#     forgepanel
#
# Then open http://localhost:8084 (default login admin / forgepanel).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# openssl lets --https generate a self-signed certificate on first start.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 forgepanel \
 && useradd --system --uid 10001 --gid forgepanel --home-dir /app --shell /usr/sbin/nologin forgepanel

WORKDIR /app
COPY . .

# cryptography enables encryption at rest (optional, but installed by default).
RUN pip install --no-cache-dir cryptography \
 && mkdir -p database custom_services scan \
 && chown -R forgepanel:forgepanel /app

USER forgepanel

EXPOSE 8084
VOLUME ["/app/database", "/app/custom_services"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8084/api/health', timeout=4).status == 200 else 1)"

# Extra flags are appended, e.g. `docker run ... forgepanel --https`
ENTRYPOINT ["python3", "server.py", "--host", "0.0.0.0", "--port", "8084"]
