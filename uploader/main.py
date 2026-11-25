
import os
import time
import json
from io import BytesIO
from uuid import uuid4
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, UploadFile, File, HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError


# Configuration
MINIO_ENDPOINT = os.getenv("MINIO_ENDPOINT", "http://minio:9000")
MINIO_BUCKET = os.getenv("MINIO_BUCKET", "images")
MINIO_ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "minioadmin123")

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

app = FastAPI(title="Uploader Service")


# MinIO S3 client
s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
    region_name="us-east-1",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"}
    )
)


# Create bucket if it does not exist
@app.on_event("startup")
def initialize_storage():
    for attempt in range(20):
        try:
            s3.head_bucket(Bucket=MINIO_BUCKET)
            break
        except ClientError:
            try:
                s3.create_bucket(Bucket=MINIO_BUCKET)
                break
            except ClientError:
                if attempt == 19:
                    raise
                time.sleep(2)

    # Gateway validates the user's JWT before fetching an object. Allow
    # anonymous reads from the bucket so Nginx can fetch that object after the
    # validation step; MinIO's S3 endpoint is only exposed inside the Docker
    # network.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{MINIO_BUCKET}/*"],
            }
        ],
    }
    s3.put_bucket_policy(Bucket=MINIO_BUCKET, Policy=json.dumps(policy))


def process_file(data: bytes):
    """
    If the uploaded file is an image, convert it to optimized JPEG.
    Otherwise, return the original file.
    """
    try:
        image = Image.open(BytesIO(data))
        image = ImageOps.exif_transpose(image)

        # JPEG does not support transparency
        if image.mode in ("RGBA", "LA"):
            background = Image.new("RGB", image.size, "white")
            if image.mode == "RGBA":
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image.convert("RGBA"),
                                 mask=image.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=80,
            optimize=True
        )

        return output.getvalue(), "image/jpeg", ".jpg"

    except (UnidentifiedImageError, OSError, ValueError):
        # Not an image: preserve original file
        return data, "application/octet-stream", ""


# POST /v1/upload - upload file
@app.post("/v1/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")

    data = await file.read(MAX_FILE_SIZE + 1)

    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File is too large")

    processed_data, content_type, extension = process_file(data)

    if extension:
        filename = f"{uuid4()}{extension}"
    else:
        original_extension = Path(file.filename).suffix[:10]
        filename = f"{uuid4()}{original_extension}"

        if file.content_type:
            content_type = file.content_type

    try:
        s3.put_object(
            Bucket=MINIO_BUCKET,
            Key=filename,
            Body=processed_data,
            ContentType=content_type
        )
    except ClientError:
        raise HTTPException(
            status_code=502,
            detail="Could not upload file to storage"
        )

    return {
        "filename": filename,
        "bucket": MINIO_BUCKET,
        "size": len(processed_data),
        "content_type": content_type
    }


@app.get("/health")
def health():
    return {"status": "ok"}
