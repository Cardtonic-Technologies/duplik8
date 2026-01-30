FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1

# install System Dependencies
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

# upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# install NumPy 2.0.0
RUN pip install --no-cache-dir "numpy<2.0.0"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# being installed from the above RUN and COPY command

# install PaddlePaddle
#RUN pip install --no-cache-dir --default-timeout=1000 paddlepaddle==2.6.2

# install Application Dependencies (MODIFIED)
# added "numpy<2.0.0" here to ensure the constraint holds during resolution
#RUN pip install --no-cache-dir --default-timeout=1000 \
#    "numpy<2.0.0" \
#    paddleocr==2.7.3 \
#    fastapi \
#    uvicorn \
#    gunicorn \
#    pillow \
#    opencv-python-headless \
#    python-multipart \
#    imagehash

# add PADDLEOCR MODELS to prevent runtime downloads/timeouts (took quite some mminute, and that's not good for oproduction)

# create the directory structure Paddle expects (root user defaults)
RUN mkdir -p /root/.paddleocr/whl/det/en/en_PP-OCRv3_det_infer \
    && mkdir -p /root/.paddleocr/whl/rec/en/en_PP-OCRv4_rec_infer \
    && mkdir -p /root/.paddleocr/whl/cls/ch_ppocr_mobile_v2.0_cls_infer

# pre-download the Detection Model (v3)
RUN wget -O /tmp/det.tar https://paddleocr.bj.bcebos.com/PP-OCRv3/english/en_PP-OCRv3_det_infer.tar \
    && tar -xf /tmp/det.tar -C /root/.paddleocr/whl/det/en/en_PP-OCRv3_det_infer --strip-components=1 \
    && rm /tmp/det.tar

# pre-download the Recognition Model (v4)
RUN wget -O /tmp/rec.tar https://paddleocr.bj.bcebos.com/PP-OCRv4/english/en_PP-OCRv4_rec_infer.tar \
    && tar -xf /tmp/rec.tar -C /root/.paddleocr/whl/rec/en/en_PP-OCRv4_rec_infer --strip-components=1 \
    && rm /tmp/rec.tar

# pre-download the Classification Model (v2.0)
RUN wget -O /tmp/cls.tar https://paddleocr.bj.bcebos.com/dygraph_v2.0/ch/ch_ppocr_mobile_v2.0_cls_infer.tar \
    && tar -xf /tmp/cls.tar -C /root/.paddleocr/whl/cls/ch_ppocr_mobile_v2.0_cls_infer --strip-components=1 \
    && rm /tmp/cls.tar

WORKDIR /app
COPY app.py .

EXPOSE 8000

ENV OMP_NUM_THREADS=1
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "--threads", "1", "--timeout", "120", "app:app", "--bind", "0.0.0.0:8000"]