FROM nvidia/cuda:12.8.1-base-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 8000
CMD ["python3","-m","uvicorn","app:app","--host","0.0.0.0","--port","8000"]
