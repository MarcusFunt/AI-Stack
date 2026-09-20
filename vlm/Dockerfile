FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ARG SOURCE_HASH=unknown
LABEL io.ai-stack.source-hash=$SOURCE_HASH
EXPOSE 8000
CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]
