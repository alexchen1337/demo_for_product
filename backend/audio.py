from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Query, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List, Optional
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import os
import uuid
import io
from datetime import datetime

from database import get_db, User, AudioFile, AudioStatus
from auth import get_current_user

try:
    from mutagen import File as MutagenFile
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False

router = APIRouter(prefix="/api/audio", tags=["audio"])

AWS_REGION = os.getenv("AWS_REGION")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "audio-files")
S3_PRESIGN_EXP_SECONDS = int(os.getenv("S3_PRESIGN_EXP_SECONDS", "7200"))
S3_AUTO_CREATE_BUCKET = os.getenv("S3_AUTO_CREATE_BUCKET", "false").lower() == "true"
S3_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "virtual")


def get_s3_client():
    if not AWS_REGION:
        raise HTTPException(status_code=500, detail="AWS region not configured")
    
    client_kwargs = {
        "region_name": AWS_REGION,
        "config": Config(s3={"addressing_style": S3_ADDRESSING_STYLE}),
    }
    
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    
    return boto3.client("s3", **client_kwargs)


def setup_s3_cors():
    allowed_origin = os.getenv("FRONTEND_URL", "*")
    rules = [
        {
            "AllowedOrigins": [allowed_origin],
            "AllowedMethods": ["GET", "HEAD"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag", "Content-Length"],
            "MaxAgeSeconds": 3600,
        }
    ]
    try:
        client = get_s3_client()
        client.put_bucket_cors(
            Bucket=S3_BUCKET_NAME,
            CORSConfiguration={"CORSRules": rules},
        )
    except Exception as e:
        # Avoid failing startup on missing permissions or credentials
        pass


def ensure_bucket_exists(client):
    try:
        client.head_bucket(Bucket=S3_BUCKET_NAME)
        return
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in ["404", "NoSuchBucket"] and S3_AUTO_CREATE_BUCKET:
            create_params = {"Bucket": S3_BUCKET_NAME}
            if AWS_REGION != "us-east-1":
                create_params["CreateBucketConfiguration"] = {"LocationConstraint": AWS_REGION}
            client.create_bucket(**create_params)
            return
        raise HTTPException(status_code=500, detail="S3 bucket not accessible")


def extract_audio_duration(file_contents: bytes, filename: str) -> Optional[int]:
    if not MUTAGEN_AVAILABLE:
        return None
    
    try:
        audio_file = MutagenFile(io.BytesIO(file_contents), easy=True)
        if audio_file and hasattr(audio_file.info, "length"):
            return int(audio_file.info.length)
    except Exception:
        return None
    
    return None


def generate_presigned_url(object_key: str):
    try:
        client = get_s3_client()
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": object_key},
            ExpiresIn=S3_PRESIGN_EXP_SECONDS,
        )
    except ClientError:
        return ""


@router.post("/upload")
async def upload_audio(
    background_tasks: BackgroundTasks,
    audio: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not audio:
        raise HTTPException(status_code=400, detail="No files provided")
    
    if len(audio) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 files per upload")
    
    uploaded_files = []
    failed_files = []
    
    try:
        s3_client = get_s3_client()
        ensure_bucket_exists(s3_client)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to storage: {str(e)}")
    
    for file in audio:
        try:
            if not file.content_type or not file.content_type.startswith("audio/"):
                failed_files.append({
                    "filename": file.filename,
                    "error": "Not an audio file"
                })
                continue
            
            if file.size and file.size > 100 * 1024 * 1024:
                failed_files.append({
                    "filename": file.filename,
                    "error": "File too large (max 100MB)"
                })
                continue
            
            file_id = str(uuid.uuid4())
            file_extension = os.path.splitext(file.filename)[1]
            object_key = f"{current_user.id}/{file_id}{file_extension}"
            
            contents = await file.read()
            if not contents:
                failed_files.append({
                    "filename": file.filename,
                    "error": "Empty file"
                })
                continue
            
            s3_client.upload_fileobj(
                io.BytesIO(contents),
                S3_BUCKET_NAME,
                object_key,
                ExtraArgs={
                    "ContentType": file.content_type,
                    "CacheControl": "public, max-age=31536000",
                },
            )
            
            file_size = len(contents)
            duration = extract_audio_duration(contents, file.filename)
            
            audio_file = AudioFile(
                id=file_id,
                user_id=current_user.id,
                object_key=object_key,
                filename=file.filename,
                file_size=file_size,
                duration=duration,
                status=AudioStatus.uploaded,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            db.add(audio_file)
            db.commit()
            db.refresh(audio_file)
            
            # trigger background transcription
            from transcription import transcribe_audio_file
            background_tasks.add_task(
                transcribe_audio_file,
                audio_file.id,
                audio_file.object_key,
                audio_file.filename
            )
            
            secure_url = generate_presigned_url(object_key)
            
            uploaded_files.append({
                "id": audio_file.id,
                "title": audio_file.filename,
                "filename": audio_file.filename,
                "url": secure_url,
                "size": audio_file.file_size,
                "duration": audio_file.duration,
                "status": audio_file.status.value,
                "uploadedAt": audio_file.created_at.isoformat(),
            })
            
        except Exception as e:
            db.rollback()
            failed_files.append({
                "filename": file.filename,
                "error": str(e)
            })
            
            try:
                s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=object_key)
            except Exception:
                pass
    
    if not uploaded_files and failed_files:
        error_details = "; ".join([f"{f['filename']}: {f['error']}" for f in failed_files])
        raise HTTPException(
            status_code=400,
            detail=f"All uploads failed - {error_details}"
        )
    
    return uploaded_files


@router.get("")
async def get_all_audio(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = None
):
    query = db.query(AudioFile).filter(AudioFile.user_id == current_user.id)
    
    if status:
        try:
            status_enum = AudioStatus[status]
            query = query.filter(AudioFile.status == status_enum)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    
    audio_files = query.order_by(AudioFile.created_at.desc()).offset(skip).limit(limit).all()
    
    return [
        {
            "id": audio.id,
            "title": audio.filename,
            "filename": audio.filename,
            "url": generate_presigned_url(audio.object_key),
            "size": audio.file_size,
            "duration": audio.duration,
            "status": audio.status.value,
            "uploadedAt": audio.created_at.isoformat(),
        }
        for audio in audio_files
    ]


@router.get("/{audio_id}")
async def get_audio(
    audio_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    audio = db.query(AudioFile).filter(
        AudioFile.id == audio_id,
        AudioFile.user_id == current_user.id
    ).first()
    
    if not audio:
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    return {
        "id": audio.id,
        "title": audio.filename,
        "filename": audio.filename,
        "url": generate_presigned_url(audio.object_key),
        "size": audio.file_size,
        "duration": audio.duration,
        "status": audio.status.value,
        "uploadedAt": audio.created_at.isoformat(),
        "updatedAt": audio.updated_at.isoformat(),
    }


@router.delete("/{audio_id}")
async def delete_audio(
    audio_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    audio = db.query(AudioFile).filter(
        AudioFile.id == audio_id,
        AudioFile.user_id == current_user.id
    ).first()
    
    if not audio:
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    try:
        s3_client = get_s3_client()
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=audio.object_key)
    except Exception:
        pass
    
    db.delete(audio)
    db.commit()
    
    return {"message": "Audio file deleted successfully"}


@router.get("/{audio_id}/test-url")
async def test_audio_url(
    audio_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    audio = db.query(AudioFile).filter(
        AudioFile.id == audio_id,
        AudioFile.user_id == current_user.id
    ).first()
    
    if not audio:
        raise HTTPException(status_code=404, detail="Audio file not found")
    
    try:
        s3_client = get_s3_client()
        s3_client.head_object(Bucket=S3_BUCKET_NAME, Key=audio.object_key)
        url = generate_presigned_url(audio.object_key)
        
        return {
            "object_key": audio.object_key,
            "url": url,
            "url_length": len(url),
        }
    except Exception as e:
        return {
            "error": str(e),
            "object_key": audio.object_key
        }
