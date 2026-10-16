# Complete Coolify Setup Guide for White-Label Voice Calling CRM

This guide walks you through setting up and deploying the Voice Calling CRM on **Coolify** (self-hosted PaaS) with automatic SSL certificates via Traefik.

---

## 📋 Prerequisites

1. A server with **Coolify** installed and running (Hetzner, DigitalOcean, Contabo, AWS, etc.).
2. Your project pushed to a Git repository (**GitHub**, **GitLab**, **Gitea**, or private Git URL).
3. A domain or subdomain (e.g. `crm.yourclientdomain.com`) with access to your DNS manager (Cloudflare, GoDaddy, Namecheap, etc.).
4. Your API credentials:
   - **LiveKit**: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`
   - **SIP / Telephony**: `OUTBOUND_TRUNK_ID`, `CALLER_ID`
   - **AI**: `GOOGLE_API_KEY` (Gemini) or Deepgram / OpenAI keys
   - **Database**: Supabase URL & Service Key (or MySQL credentials)
   - **WhatsApp** (optional): Meta Cloud API credentials

---

## 🚀 Deployment Methods

You have two simple options in Coolify:
- **Option 1 (Recommended)**: **Dockerfile Deployment** (Fastest, zero-config, works with external Supabase or Coolify-managed Database).
- **Option 2**: **Docker Compose Deployment** (If you want a unified compose file).

---

## Option 1: Standard Coolify Deployment (Dockerfile - Recommended)

### Step 1: Add New Application in Coolify

1. Open your **Coolify Dashboard**.
2. Navigate to **Projects** → Select your Project → Select an Environment (e.g., `production`).
3. Click **+ New Resource** → **Application**.
4. Select your Git provider:
   - **GitHub App** or **Public Repository** (or Private Repository with deploy key).
5. Select this repository and set branch to `main`.

---

### Step 2: General & Build Configuration

Coolify will inspect the repository and detect the `Dockerfile`. Verify the following settings in the **General** tab:

| Setting | Value | Notes |
| :--- | :--- | :--- |
| **Build Pack** | `Dockerfile` | Coolify auto-detects `Dockerfile` at root |
| **Base Directory** | `/` | Root of repo |
| **Dockerfile Location** | `/Dockerfile` | Default |
| **Ports Exposes** | `8000` | The internal FastAPI port |
| **Domains** | `https://crm.yourdomain.com` | Replace with your actual domain; Traefik provisions Let's Encrypt SSL automatically |

---

### Step 3: Configure Health Check (Critical for Traefik)

To prevent Traefik's `"502 Bad Gateway"` or `"no available server"` errors:

1. Go to the **Health Checks** section in Coolify.
2. Set the following parameters:
   - **Healthcheck Path**: `/api/live`
   - **Port**: `8000`
   - **Interval**: `30` seconds
   - **Timeout**: `5` seconds
   - **Retries**: `3`
   - **Start Period (Grace Period)**: `20` to `30` seconds (allows FastAPI + LiveKit agent to connect before checking)

> 💡 **Why `/api/live`?**  
> `/api/live` is an ultra-lightweight liveness check that responds in `<5ms` without touching databases or external APIs. This ensures Traefik always knows your container is healthy.

---

### Step 4: Add Environment Variables

In Coolify, go to the **Environment Variables** tab of your application.  
Copy and paste the template below (adjusting values for your client):

```env
# Server & Runtime
PORT=8000
PYTHONUNBUFFERED=1
ENVIRONMENT=production

# Client Branding
BUSINESS_NAME="Apex Dental Clinic"
BUSINESS_TYPE="Dental Clinic"
DEFAULT_SERVICE_TYPE="Dental Consultation"
BUSINESS_CITY="Bangalore"
BUSINESS_PHONE="+919876543210"
SUPPORT_EMAIL="support@apexdental.com"
APP_TIMEZONE="Asia/Kolkata"
APP_COUNTRY="India"
APP_COUNTRY_CODE="+91"

# Telephony & LiveKit (SIP / WebRTC)
LIVEKIT_URL="wss://livekit.yourdomain.com"
LIVEKIT_API_KEY="your_livekit_api_key"
LIVEKIT_API_SECRET="your_livekit_api_secret"
OUTBOUND_TRUNK_ID="client_outbound_trunk_id"
CALLER_ID="+919876543210"

# AI Voice & LLM Models
GOOGLE_API_KEY="AIzaSy..."
GEMINI_MODEL="gemini-3.1-flash-live-preview"
GEMINI_TTS_VOICE="Kore"
USE_GEMINI_REALTIME="true"
DEEPGRAM_API_KEY=""

# Database (Supabase / PostgreSQL)
AIRZEN_DB_BACKEND="supabase"
SUPABASE_URL="https://xyzcompany.supabase.co"
SUPABASE_SERVICE_KEY="eyJhbGciOi..."

# Or if using MySQL:
# AIRZEN_DB_BACKEND="mysql"
# MYSQL_HOST="127.0.0.1"
# MYSQL_PORT="3306"
# MYSQL_DATABASE="airzen"
# MYSQL_USER="airzen_app"
# MYSQL_PASSWORD="your_mysql_password"

# WhatsApp Cloud API (Optional / Meta)
WHATSAPP_PHONE_NUMBER_ID=""
WHATSAPP_ACCESS_TOKEN=""
WHATSAPP_BUSINESS_ACCOUNT_ID=""
WHATSAPP_WEBHOOK_VERIFY_TOKEN="your_random_secure_verify_token"

# Admin Security
AUTH_ENABLED="false"
ADMIN_API_KEY="your_secret_admin_key"
ADMIN_DEFAULT_PASSWORD="ClientAdminPassword2026!"
```

---

### Step 5: Configure DNS in your Domain Registrar / Cloudflare

1. Open your DNS provider (Cloudflare, Namecheap, GoDaddy, Route53, etc.).
2. Add an **A Record**:
   - **Type**: `A`
   - **Name**: `crm` (for `crm.yourdomain.com`) or `@` (for root domain)
   - **Content / Value**: `YOUR_VPS_IP_ADDRESS`
   - **Proxy status**: DNS only (Grey Cloud) recommended during initial SSL issuance.

---

### Step 6: Deploy Application

1. In Coolify, click **Deploy**.
2. Watch the **Deployment Logs**:
   - Docker image installs dependencies from `requirements.txt`.
   - Starts container with `start.sh` (launches FastAPI on port 8000 and the LiveKit agent worker).
3. Once deployed, verify in container logs:
   - `🚀 Starting OutboundAI...`
   - `🌐 Starting FastAPI on port 8000...`
   - `🤖 Starting LiveKit agent worker...`

---

## 🔍 Verification & Diagnostics

Once deployed, test the following endpoints:

| Endpoint | Purpose | Expected Response |
| :--- | :--- | :--- |
| `https://crm.yourdomain.com/` | Web Dashboard / CRM UI | Loads the CRM portal interface |
| `https://crm.yourdomain.com/api/live` | Lightweight Traefik Health Check | `{"status": "alive", "uptime_seconds": ...}` |
| `https://crm.yourdomain.com/api/ready` | Readiness (DB & External checks) | `{"status": "ready", ...}` |
| `https://crm.yourdomain.com/api/runtime/health` | Comprehensive Diagnostics | Detailed memory, FD count, WhatsApp status |

---

## 💬 WhatsApp Webhook Setup (Meta Developer Portal)

If WhatsApp is enabled:
1. Open [Meta for Developers](https://developers.facebook.com/) → WhatsApp → Configuration.
2. **Callback URL**: `https://crm.yourdomain.com/api/whatsapp/webhook`
3. **Verify Token**: Must match `WHATSAPP_WEBHOOK_VERIFY_TOKEN` set in your Coolify environment variables.
4. Click **Verify and Save**.
5. Subscribe to the `messages` webhook field.

---

## 🛠️ Common Coolify Troubleshooting

### 1. "Bad Gateway" or "No Available Server"
- Check that **Ports Exposes** in Coolify is set to `8000`.
- Verify the container health check path is `/api/live`.
- In Coolify, check container logs to verify `uvicorn server:app` is running without crashes.

### 2. LiveKit Agent Worker Reconnection
- `start.sh` contains an automatic watchdog loop. If the LiveKit worker ever disconnects, the watchdog restarts it automatically within 5 seconds without restarting the web server.

### 3. Restart Policy
- In Coolify Application Settings, ensure **Restart Policy** is set to `unless-stopped` or `always`.
