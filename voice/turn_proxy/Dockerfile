FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY __init__.py /app/turn_proxy/__init__.py
COPY proxy.py /app/turn_proxy/proxy.py

ENTRYPOINT ["python", "/app/turn_proxy/proxy.py"]
