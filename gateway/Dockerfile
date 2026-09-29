FROM python:3.12-slim
WORKDIR /app
COPY gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway
COPY core ./core
COPY observability ./observability
ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH
EXPOSE 8000
CMD ["uvicorn","gateway.app:app","--host","0.0.0.0","--port","8000"]
