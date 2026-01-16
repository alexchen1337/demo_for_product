from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import hashlib
import os
import secrets
import time
import uuid
import httpx
from fastapi import APIRouter, HTTPException, Response, Cookie, Depends
from fastapi.responses import RedirectResponse
from jose import jwt, JWTError, ExpiredSignatureError
from sqlalchemy.orm import Session
from dotenv import load_dotenv
from urllib.parse import urlencode

from database import get_db, User, Session as DBSession

ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=ROOT_ENV)

router = APIRouter(prefix="/auth", tags=["authentication"])

FRONTEND_URL = os.getenv("FRONTEND_URL")
BACKEND_URL = os.getenv("BACKEND_URL")

COGNITO_DOMAIN = os.getenv("COGNITO_DOMAIN")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
AWS_REGION = os.getenv("AWS_REGION")

REDIRECT_URI = f"{BACKEND_URL}/auth/cognito/callback"
SCOPES = ["openid", "email"]
ISSUER = f"https://cognito-idp.{AWS_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("JWT_SECRET environment variable must be set")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 30

IS_DEVELOPMENT = FRONTEND_URL and ("localhost" in FRONTEND_URL or "127.0.0.1" in FRONTEND_URL)

_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


def ensure_cognito_config():
    if not all([COGNITO_DOMAIN, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET, COGNITO_USER_POOL_ID, AWS_REGION]):
        raise HTTPException(status_code=500, detail="Cognito configuration missing")


async def get_jwks_keys():
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < 3600:
        return _jwks_cache["keys"]
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(JWKS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _jwks_cache["keys"] = data.get("keys", [])
        _jwks_cache["fetched_at"] = now
        return _jwks_cache["keys"]


async def decode_cognito_id_token(id_token: str, access_token: str = None):
    try:
        keys = await get_jwks_keys()
        headers = jwt.get_unverified_header(id_token)
        kid = headers.get("kid")
        key = next((k for k in keys if k.get("kid") == kid), None)
        if not key:
            raise HTTPException(status_code=401, detail="Signing key not found")
        
        return jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=COGNITO_CLIENT_ID,
            issuer=ISSUER,
            access_token=access_token,
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid ID token")


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(64)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def get_current_user(
    access_token: Optional[str] = Cookie(None, alias="access_token"),
    db: Session = Depends(get_db)
) -> User:
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(access_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        db.commit()
        return user
        
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.get("/cognito/login")
async def cognito_login():
    ensure_cognito_config()
    
    state = secrets.token_urlsafe(32)
    
    params = {
        "client_id": COGNITO_CLIENT_ID,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    auth_url = f"{COGNITO_DOMAIN}/oauth2/authorize?{urlencode(params)}"
    
    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=not IS_DEVELOPMENT,
        samesite="lax",
        max_age=600,
        path="/",
    )
    return response


@router.get("/cognito/callback")
async def cognito_callback(
    code: str,
    response: Response,
    state: Optional[str] = None,
    oauth_state: Optional[str] = Cookie(None, alias="oauth_state"),
    db: Session = Depends(get_db)
):
    try:
        if not state or not oauth_state or not secrets.compare_digest(state, oauth_state):
            raise HTTPException(status_code=400, detail="Invalid state parameter")
        
        ensure_cognito_config()
        token_url = f"{COGNITO_DOMAIN}/oauth2/token"
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "client_id": COGNITO_CLIENT_ID,
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                },
                auth=(COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if token_resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Token exchange failed")
            
            tokens = token_resp.json()
            id_token = tokens.get("id_token")
            cognito_access_token = tokens.get("access_token")
            if not id_token or not cognito_access_token:
                raise HTTPException(status_code=400, detail="Tokens missing in response")
            
            claims = await decode_cognito_id_token(id_token, cognito_access_token)
        
        provider_id = claims.get("sub")
        email = claims.get("email")
        name = claims.get("name") or claims.get("cognito:username")
        
        if not provider_id:
            raise HTTPException(status_code=400, detail="Invalid identity")
        
        user = db.query(User).filter(User.identity_provider_id == provider_id).first()
        
        if not user and email:
            user = db.query(User).filter(User.email == email).first()
        
        if not user:
            user = User(
                id=str(uuid.uuid4()),
                identity_provider_id=provider_id,
                email=email,
                name=name,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(user)
        else:
            user.identity_provider_id = provider_id
            user.email = email or user.email
            user.name = name or user.name
            user.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(user)
        
        jwt_access_token = create_access_token(
            {"user_id": user.id, "email": user.email}
        )
        refresh_token = create_refresh_token()
        
        session = DBSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            refresh_token=hash_refresh_token(refresh_token),
            expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
            created_at=datetime.utcnow()
        )
        db.add(session)
        db.commit()
        
        frontend_callback = f"{FRONTEND_URL}/auth/callback?success=true"
        redirect_response = RedirectResponse(url=frontend_callback)
        
        redirect_response.delete_cookie(
            key="oauth_state",
            httponly=True,
            secure=not IS_DEVELOPMENT,
            samesite="lax",
            path="/",
        )
        
        redirect_response.set_cookie(
            key="access_token",
            value=jwt_access_token,
            httponly=True,
            secure=not IS_DEVELOPMENT,
            samesite="lax",
            max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            path="/",
        )
        redirect_response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=not IS_DEVELOPMENT,
            samesite="lax",
            max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
            path="/",
        )
        
        return redirect_response
        
    except HTTPException as e:
        error_url = f"{FRONTEND_URL}/auth/callback?error=authentication_failed"
        return RedirectResponse(url=error_url)
    except Exception:
        error_url = f"{FRONTEND_URL}/auth/callback?error=authentication_failed"
        return RedirectResponse(url=error_url)


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "role": current_user.role,
        "organization": current_user.organization,
        "group": current_user.group,
    }


@router.post("/refresh")
async def refresh_access_token(
    response: Response,
    refresh_token: Optional[str] = Cookie(None, alias="refresh_token"),
    db: Session = Depends(get_db)
):
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token not found")
    
    token_hash = hash_refresh_token(refresh_token)
    session = db.query(DBSession).filter(
        DBSession.refresh_token == token_hash,
        DBSession.expires_at > datetime.utcnow()
    ).first()
    
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    
    user = db.query(User).filter(User.id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    new_access_token = create_access_token(
        {"user_id": user.id, "email": user.email}
    )
    
    # rotate refresh token to prevent replay attacks
    new_refresh_token = create_refresh_token()
    session.refresh_token = hash_refresh_token(new_refresh_token)
    session.expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    db.commit()
    
    response.set_cookie(
        key="access_token",
        value=new_access_token,
        httponly=True,
        secure=not IS_DEVELOPMENT,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=not IS_DEVELOPMENT,
        samesite="lax",
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
        path="/",
    )
    
    return {"message": "Token refreshed successfully"}


@router.post("/logout")
async def logout(
    response: Response,
    refresh_token: Optional[str] = Cookie(None, alias="refresh_token"),
    db: Session = Depends(get_db)
):
    if refresh_token:
        token_hash = hash_refresh_token(refresh_token)
        db.query(DBSession).filter(DBSession.refresh_token == token_hash).delete()
        db.commit()
    
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=not IS_DEVELOPMENT,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=not IS_DEVELOPMENT,
        samesite="lax",
        path="/",
    )
    
    return {"message": "Logged out successfully"}

