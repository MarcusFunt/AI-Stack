FROM python:3.12-slim
WORKDIR /app
COPY mcp/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp/app.py ./app.py
COPY mcp/outbound.py ./outbound.py
COPY core ./core
COPY observability ./observability
ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000","--proxy-headers","--forwarded-allow-ips","*"]
