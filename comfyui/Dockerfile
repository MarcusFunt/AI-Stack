FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
WORKDIR /opt/ComfyUI
COPY third_party/ComfyUI/ /opt/ComfyUI/
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8188
CMD ["python","main.py","--listen","0.0.0.0","--port","8188","--lowvram"]
