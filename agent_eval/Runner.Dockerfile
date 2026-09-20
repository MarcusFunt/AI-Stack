FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY runner_runtime.py /app/runner_runtime.py

ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH

CMD ["python", "/app/runner_runtime.py", "daemon"]
