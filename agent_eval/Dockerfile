FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app/agent_eval

ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH

EXPOSE 8000
CMD ["uvicorn", "agent_eval.app:app", "--host", "0.0.0.0", "--port", "8000"]
