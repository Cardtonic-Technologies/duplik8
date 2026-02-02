# Duplik8 OCR Service

A high-performance, containerized Optical Character Recognition (OCR) microservice designed to extract raw text from images. This service acts as the "Eyes" of the **Duplik8** ecosystem, relying on **PaddleOCR** to perform detection and recognition while delegating business logic (parsing, regex, validation) to upstream services (Temporal/Node.js).

## 🚀 Key Features

* **Engine:** Powered by [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) (PP-OCRv4).
* **Production Ready:** Uses `Gunicorn` (Process Manager) + `Uvicorn` (ASGI Worker) for concurrency.
* **Kubernetes Optimized:**
* **Zero-Latency Startup:** OCR models are "baked" into the Docker image, preventing runtime download failures and 3+ minute startup delays (cold startups).
* **Stateless:** No local storage required; processes URL inputs purely in memory.


* **Stability:** Explicit dependency pinning prevents the common `numpy` 2.x breaking changes.

---

## 🛠 Tech Stack

* **Language:** Python 3.10 (Slim)
* **Framework:** FastAPI
* **OCR Library:** PaddlePaddle 2.6.2 & PaddleOCR 2.7.3
* **Computer Vision:** OpenCV (headless)
* **Server:** Gunicorn + Uvicorn Workers

> [!WARNING]
> 
> Hasn't been tested with latest 3.x versions, few test that was done kept failing, and `PaddleOCR` phones home, though for model updates, which might be a privacy concern

---

## 🐳 Quick Start

### Prerequisites

* Docker Engine
* Docker Compose

### Running Locally

1. **Build and Start:**
```bash
docker-compose up --build

```

> [!NOTE]
> 
> Authentication can be added as below
> 
> ```yaml
> services:
>   duplik8:
>     build:
>       context: .
>       dockerfile: Dockerfile
>     ports:
>       - "8000:8000"
>     environment:
>       - BASIC_AUTH_USERNAME=yourusername
>       - BASIC_AUTH_PASSWORD=yourpassword
>     restart: always
> ```



2. **Verify Status:**
The service runs on port `8000`. Check the health endpoint:
```bash
curl http://localhost:8000/
# Output: {"status":"ok","message":"OCR Service is Ready"}

```



### Testing with an Image

You can test the OCR engine using `curl` or Postman.

**Endpoint:** `POST /analyze`

```bash
curl -X POST http://localhost:8000/analyze \
     -H "Content-Type: application/json" \
     -d '{"image_url": "https://assets.pil.com/1.jpeg"}'

```
**With Auth:** `POST /analyze`

```bash
curl -u yourusername:yourpassword \
     -H "Content-Type: application/json" \
     -d '{"image_url":"https://assets.pil.com/1.jpeg"}' \
     http://localhost:8000/analyze


```

---

## 📡 API Reference

### 1. Health Check

* **URL:** `/`
* **Method:** `GET`
* **Description:** Used by Kubernetes Liveness/Startup probes to verify the container is responsive.

### 2. Analyze Image

* **URL:** `/analyze`
* **Method:** `POST`
* **Description:** Downloads an image from a URL and returns extracted text blocks with confidence scores.

#### Request Body

```json
{
  "image_url": "https://example.com/image.jpg"
}

```

#### Response Body

```json
{
  "message": "Analysis successful",
  "count": 2,
  "data": [
    {
      "text": "STEAM",
      "confidence": 0.9969,
      "box": [[335, 750], [715, 767], [714, 787], [334, 770]]
    },
    {
      "text": "XR56L-H4A5Q-87YXF",
      "confidence": 0.9690,
      "box": [[127, 839], [512, 854], [511, 882], [126, 867]]
    }
  ]
}

```

---

## 🏗 Architecture & Decisions

### 1. The "Dumb Container" Philosophy

This service is intentionally designed to be "dumb."

* **It DOES** extract text from pixels.
* **It DOES NOT** know what a "Steam Card" is, validate formats, or run Regex.
* **Reasoning:** This allows us to update card formats and regex patterns in the upstream Node.js/Temporal workflow without rebuilding and redeploying this heavy Python container.

### 2. Dependency Pinning (`NumPy < 2.0`)

**⚠️ CRITICAL WARNING:** Do not upgrade NumPy to 2.x.
PaddleOCR and OpenCV currently have binary incompatibilities with NumPy 2.0+. The Dockerfile explicitly enforces:

```dockerfile
RUN pip install ... "numpy<2.0.0"

```

Removing this constraint will cause the container to crash with `C-API` mismatch errors during initialization.

### 3. Model "Baking"

By default, PaddleOCR downloads models (~20MB) at **runtime** (first request).

* **Problem:** This causes initial requests to timeout (taking 2-3 minutes) and can crash Kubernetes pods due to liveness probe failures.
* **Solution:** We use `wget` in the `Dockerfile` to download the following models during the **build phase**:
1. Detection: `en_PP-OCRv3_det_infer`
2. Recognition: `en_PP-OCRv4_rec_infer`
3. Classification: `ch_ppocr_mobile_v2.0_cls_infer`



These are stored in `/root/.paddleocr` so the app starts instantly.

---

## 🚢 Kubernetes Deployment Guide

When deploying to the DOKS (DigitalOcean Kubernetes) cluster:

### Resource Limits (Recommended)

OCR is CPU intensive. Ensure these minimums are met to prevent starvation.

```yaml
resources:
  requests:
    cpu: "500m"
    memory: "1Gi"
  limits:
    cpu: "2000m"
    memory: "2Gi"

```

### Probes

Since models are pre-baked, startup is fast, but allow a small buffer for Gunicorn workers to boot.

* **Liveness Probe:** `/` (port 8000)
* **Startup Probe:** `/` (port 8000, initialDelay: 5s)

### Scaling

Deploy as a `Deployment` kind with **HPA** (Horizontal Pod Autoscaler).

* **Min Replicas:** 3 (For high availability)
* **Target CPU:** 70%

---

## 🧪 Troubleshooting

**Issue:** `422 Unprocessable Entity`

* **Cause:** You are sending `form-data` or a raw string instead of JSON.
* **Fix:** Ensure the `Content-Type` is `application/json` and the body is `{"image_url": "..."}`.

**Issue:** `ModuleNotFoundError: No module named 'numpy.core.multiarray'`

* **Cause:** NumPy 2.0 was installed by accident.
* **Fix:** Check `Dockerfile` step 5 and ensure `"numpy<2.0.0"` is present.

**Issue:** `Download error` or `Timeout` inside the logs.

* **Cause:** The container cannot reach the image URL provided.
* **Fix:** Ensure the URL is public or the container has network access to the asset server.