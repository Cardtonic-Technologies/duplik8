from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from paddleocr import PaddleOCR
import cv2
import numpy as np
import requests
import os
import secrets
from typing import Optional

app = FastAPI()

# basic authentication (this is optional, and only fires with the environments are provided)
security = HTTPBasic(auto_error=False)

BASIC_AUTH_USERNAME = os.getenv("BASIC_AUTH_USERNAME")
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD")


def optional_basic_auth(
    credentials: Optional[HTTPBasicCredentials] = Depends(security),
):
    # No auth configured → open access
    if not BASIC_AUTH_USERNAME and not BASIC_AUTH_PASSWORD:
        return

    # Auth configured but no credentials sent
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Validate username (if configured)
    if BASIC_AUTH_USERNAME and not secrets.compare_digest(
        credentials.username, BASIC_AUTH_USERNAME
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # Validate password (if configured)
    if BASIC_AUTH_PASSWORD and not secrets.compare_digest(
        credentials.password, BASIC_AUTH_PASSWORD
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

# We do this outside the function so the model loads only once at startup.
# use_angle_cls=True loads the direction classifier model.
ocr_engine = PaddleOCR(use_angle_cls=True, lang='en')


class AnalysisRequest(BaseModel):
    image_url: str

@app.get("/")
def health_check():
    return {"status": "ok", "message": "duplik8 Service is Ready"}


@app.post("/analyze")
def analyze_image(
    payload: AnalysisRequest,
    _=Depends(optional_basic_auth),
):
    """
    Receives an image_url, downloads the image, and runs OCR.
    """
    image_url = payload.image_url

    # download the image
    try:
        response = requests.get(image_url, timeout=10)
        response.raise_for_status()  # Raise error for 404/500 responses
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to download image: {str(e)}")

    # decode image to Numpy Array (Required by PaddleOCR/OpenCV)
    try:
        # convert raw bytes to numpy array
        image_bytes = np.frombuffer(response.content, np.uint8)
        # decode image using OpenCV
        img = cv2.imdecode(image_bytes, cv2.IMREAD_COLOR)

        if img is None:
            raise HTTPException(status_code=400, detail="URL did not point to a valid image file.")

    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Image processing failed: {str(e)}")

    # run OCR Analysis
    # cls=True enables angle classification
    result = ocr_engine.ocr(img, cls=True)

    # parse Results
    # PaddleOCR returns a list of lists. If no text is found, it can be None or empty.
    extracted_data = []

    if result and result[0]:
        for line in result[0]:
            coords = line[0]
            text, confidence = line[1]

            extracted_data.append({
                "text": text,
                "confidence": round(confidence, 4),
                "box": coords
            })

    return {
        "message": "Analysis successful",
        "count": len(extracted_data),
        "data": extracted_data
    }
