# FastAPI Backend - Marci Audio Manager

FastAPI backend with AWS Cognito authentication, RDS SQL Server, and S3-backed audio management.

## Features

- OAuth 2.0 authentication via AWS Cognito hosted UI
- JWT token-based session management (HTTP-only cookies)
- Audio upload/list/delete backed by Amazon S3 with presigned URLs
- SQL Server persistence (Amazon RDS for SQL Server)
- CORS-enabled for frontend integration

## Setup

### 1) Install dependencies

```bash
pip install -r requirements.txt
```

### 2) Environment variables

- `AWS_REGION` - e.g. `us-east-1`
- `COGNITO_DOMAIN` - e.g. `https://your-domain.auth.us-east-1.amazoncognito.com`
- `COGNITO_CLIENT_ID` / `COGNITO_CLIENT_SECRET` / `COGNITO_USER_POOL_ID`
- `S3_BUCKET_NAME` - target bucket for audio objects
- `S3_AUTO_CREATE_BUCKET` - `true` to auto-create if missing (optional)
- `S3_PRESIGN_EXP_SECONDS` - presigned URL lifetime (default 7200)
- `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_PORT` - RDS SQL Server connection
- `JWT_SECRET` - secret for app-issued JWT cookies
- `FRONTEND_URL` / `BACKEND_URL` - service URLs used for redirects/CORS

Alternatively set `DATABASE_URL` for a complete SQLAlchemy URL.

### 3) Run the server

```bash
uvicorn main:app --reload --port 8000
```

Or using Python:

```bash
python main.py
```

## API Endpoints

### Authentication

- `GET /auth/cognito/login` - initiate Cognito hosted UI flow
- `GET /auth/cognito/callback` - OAuth callback handler
- `GET /auth/me` - current user info (cookie-based)
- `POST /auth/logout` - logout

### Audio Management (auth required)

- `POST /api/audio/upload` - upload audio files (multipart/form-data)
- `GET /api/audio` - list audio files for current user
- `DELETE /api/audio/{id}` - delete an audio file

### General

- `GET /` - API status
- `GET /health` - health check
- `GET /docs` - Swagger UI

## File Storage

Audio objects are stored in S3 under `userId/{uuid}{ext}`. Responses return presigned URLs for access.

## Security Notes

- Use HTTPS in production
- Keep `JWT_SECRET`, Cognito secrets, and DB credentials in a secret manager (AWS Secrets Manager/SSM)
- Restrict S3 bucket public access; presigned URLs are short-lived

## Testing

Visit `http://localhost:8000/docs` for interactive API documentation.

