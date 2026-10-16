# White-Label Voice Calling CRM: Client Onboarding & Deployment Guide

This folder (`Voicecallcrm-whitelabel-template`) is your standalone, ready-to-deploy master template for launching AI Voice Calling CRMs for any business vertical on their own domain and VPS/Coolify hosting.

---

## Supported Industry Verticals Ready Out-of-the-Box

1. **🦷 Dental Clinics & Healthcare / Hospitals**
   - **Primary Action**: AI qualifies patient pain/symptoms, triages urgency, and books doctor consultation slots.
   - **Preset**: `dental` (New Patient -> Symptoms Evaluated -> Slot Confirmed -> Treated -> Follow-up Due).

2. **🎨 Interior Design & Architecture**
   - **Primary Action**: AI qualifies apartment size (2BHK/3BHK), budget (₹5L-₹20L+), possession date, and books 3D design / site measurement visits.
   - **Preset**: `interior` (New Lead -> Site Measurement -> 3D Layout -> Quotation -> Advance -> Handover).

3. **🏡 Real Estate Agencies & Brokers**
   - **Primary Action**: AI qualifies home buyers (budget, self-use vs investment, preferred locality) and schedules property site visits.
   - **Preset**: `realestate` (Lead Received -> Qualified -> Brochure Sent -> Site Visit Booked -> Negotiation -> Token Paid).

4. **🏗️ Builders & Construction Developers**
   - **Primary Action**: AI handles high-volume project launch ads, delivers digital brochures on WhatsApp, and books VIP experience centre tours.
   - **Preset**: `builder` (Campaign Lead -> Brochure Delivered -> Lounge Tour Booked -> Unit Blocked -> Agreement Signed).

---

## 4-Step Quick Deployment for a New Client

### Step 1: Copy this folder or push to a new Git repository
For a new client (e.g. *Smile Dental Clinic*), initialize a new repo:
```bash
git init
git add .
git commit -m "Initial commit for Smile Dental Clinic CRM"
git remote add origin https://github.com/your-account/smile-dental-crm.git
git push -u origin main
```

### Step 2: Configure Client `.env`
Copy `.env.example` to `.env` and fill in:
- `BUSINESS_NAME="Smile Dental Clinic"`
- `BUSINESS_TYPE="Dental Clinic"`
- `DEFAULT_SERVICE_TYPE="Dental Consultation"`
- `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `OUTBOUND_TRUNK_ID`
- `DATABASE_URL` (MySQL or PostgreSQL connection string)
- `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`

### Step 3: Deploy on Coolify / VPS
1. Connect Coolify to `smile-dental-crm`.
2. Add the `.env` variables in Coolify.
3. Map domain: `crm.smiledentalclinic.com`.
4. Click **Deploy** (Traefik automatically provisions SSL).

### Step 4: Map DNS
Ask client to add one `A` record in their DNS:
- Type: `A`
- Name: `crm`
- Value: `YOUR_VPS_IP`

The client's CRM is now 100% operational!
