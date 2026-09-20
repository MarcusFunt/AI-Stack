FROM python:3.12-slim
ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
