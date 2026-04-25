# Nexus 001 — Deployment Guide

## Backend Environment Variables (Render)
```
MONGO_URL=mongodb+srv://...your atlas url...
DB_NAME=nexus001
ENCRYPTION_KEY=8HSvWVEhVykitb1Oy5oRm9SjLH84PXK7VgtPHUDI274=
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
FRONTEND_URL=https://your-app.vercel.app
BACKEND_URL=https://your-backend.onrender.com
```

## Render Backend Settings
- Root Directory: backend
- Language: Python
- Build Command: pip install -r requirements.txt
- Start Command: uvicorn server:app --host 0.0.0.0 --port $PORT

## Frontend Environment Variables (Vercel)
```
REACT_APP_BACKEND_URL=https://your-backend.onrender.com
```

## Vercel Frontend Settings
- Root Directory: frontend
- Build Command: yarn build
- Output Directory: build

## Google OAuth Setup
1. console.cloud.google.com → New Project → Nexus001
2. APIs & Services → OAuth Consent Screen → External
3. Credentials → Create OAuth Client ID → Web Application
4. Authorized redirect URIs: https://your-backend.onrender.com/api/auth/google/callback
5. Copy Client ID and Client Secret → paste in Render env vars
