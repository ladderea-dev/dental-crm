# Smile Dental and Implant Centre - AI Voice & WhatsApp CRM

Dedicated AI-powered Voice Calling, WhatsApp CRM, and Appointment Management platform tailored specifically for **Smile Dental and Implant Centre** (Kapra, ECIL, AS Rao Nagar, Secunderabad).

---

## 1. Clinic Profile & Knowledge Base Overview

- **Clinic Name:** Smile Dental and Implant Centre
- **Official Website:** [smiledentalandimplantcentre.com](https://smiledentalandimplantcentre.com/)
- **Google Maps Location:** [View on Google Maps](https://maps.google.com/?cid=11928096181775791226)
- **Clinic Address:**  
  `H.No. 1-1-308/8, Plot No. 8, 1st Floor, Above K. Rathod Jewellers, Sri Ram Nagar Colony, Near Kapra Circle, Saket Road, ECIL, Near AS Rao Nagar, Hyderabad / Secunderabad – 500062, Telangana, India`
- **Landmark:** Above K. Rathod Jewellers, right at Sri Ram Nagar Colony, near Kapra Circle.
- **Contact Numbers:**  
  - Primary / WhatsApp: `+91 7396529265`  
  - Reception Desk: `+91 9490618635` / `+91 9490618636`
- **Email:** `smiledentalandimplantcentre@gmail.com`
- **Operating Hours:**  
  - **Monday to Saturday:** 9:00 AM – 9:00 PM  
  - **Sunday:** 10:00 AM – 1:00 PM (Prior appointments & emergencies)
- **Chief Doctors & Specialists:**  
  - **Dr. P. Sudhakar, BDS, MDS** – Senior Periodontist & Implantologist (15+ Years Experience)  
  - **Dr. K. Mounika, BDS, PGDCC** – Cosmetic Dentist & Facial Aesthetics Specialist
- **Key Specializations:**  
  - Single-Sitting Painless Root Canal Treatment (Rotary Endodontics & Apex Locator)  
  - Dental Implants & Full Mouth Rehabilitation (All-on-4 / All-on-6, Basal & Immediate loading)  
  - Invisalign & Clear Aligners / Orthodontic Braces  
  - Cosmetic Dentistry & Smile Designing (Porcelain Veneers, 1-Hour Teeth Whitening)  
  - Metal-Free Zirconia Crowns & Bridges (5 to 15-Year Warranty)  
  - Ultrasonic Teeth Cleaning, Scaling & Gum Therapy  
  - Wisdom Tooth Extraction & Oral Surgery  
  - Pediatric Dental Care & Habit Breaking  
  - Complete & Flexible Partial Dentures

---

## 2. Pre-Configured Demo Accounts & Credentials

The system comes pre-seeded with role-based access accounts ready for client demonstration:

| Account Type | Username | Password | Role | Permissions |
| :--- | :--- | :--- | :--- | :--- |
| **Clinic Admin (MD)** | `admin` | `SmileDental@2026` | `admin` | Full system access: CRM, Telecaller workspace, WhatsApp AI, Knowledge Base, Staff Doctors, Settings, Reports, User Management. |
| **Reception / Telecaller** | `reception` | `SmileDesk@2026` | `telecaller` | Calling dashboard, lead intake, appointment booking, WhatsApp customer conversations, follow-up queues. |
| **Chief Doctor** | `dr_sudhakar` | `SmileDoc@2026` | `manager` | Patient history review, clinical appointments, pipeline progress, consultation outcome tracking. |
| **Cosmetic Specialist** | `dr_mounika` | `SmileDoc@2026` | `manager` | Patient records, cosmetic appointments, smile design consultations, pipeline updates. |

---

## 3. Local Verification & Testing

To test or run a local demo:

1. **Verify Setup:**
   ```bash
   python test_smiledental_setup.py
   python test_server_branding.py
   ```

2. **Launch the Server Locally:**
   ```bash
   python -m uvicorn server:app --host 0.0.0.0 --port 8000
   ```
   Open your browser at: `http://localhost:8000`

---

## 4. Pushing to GitHub

To push this project to a new Git repository:

```bash
# Navigate to the project directory
cd D:\Vloicecallcrm-main\smiledental-voicecallcrm

# Initialize git (already initialized)
git add .
git commit -m "Initial release: Smile Dental and Implant Centre AI Voice & WhatsApp CRM"

# Add your GitHub remote repository (replace with your repo URL)
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/smiledental-voicecallcrm.git

# Push code
git push -u origin main
```

---

## 5. Coolify Deployment Step-by-Step

### Step 1: Create Application in Coolify
1. Log in to your Coolify dashboard.
2. Select your Project / Environment.
3. Click **+ New Resource** -> **Public/Private GitHub Repository**.
4. Select `smiledental-voicecallcrm` (main branch).
5. Choose **Dockerfile** as build pack (uses the included production Dockerfile).

### Step 2: Configure MySQL Database
1. In Coolify, create a **MySQL Database** resource (or use an existing MySQL server).
2. Create database: `smiledental_crm`.
3. Run the initialization script:
   - Connect to MySQL using phpMyAdmin, DBeaver, or Coolify Terminal.
   - Run the included `smiledental_mysql_init.sql` script.
   - This automatically configures:
     - All 9 dental pipeline stages
     - Dr. P. Sudhakar and Dr. K. Mounika in `appointment_staff`
     - The complete Smile Dental Knowledge Base (`KNOWLEDGE_BASE_JSON`)
     - Pre-configured demo user accounts

### Step 3: Configure Environment Variables in Coolify
In the Coolify application **Environment Variables** tab, add:

```env
PORT=8000
ENVIRONMENT=production
PYTHONUNBUFFERED=1

# Brand & Localization
BUSINESS_NAME=Smile Dental and Implant Centre
BUSINESS_TYPE=Dental Clinic & Implant Centre
DEFAULT_SERVICE_TYPE=Dental Consultation
BUSINESS_CITY=Secunderabad / Hyderabad
APP_TIMEZONE=Asia/Kolkata
APP_COUNTRY=India
APP_COUNTRY_CODE=+91

# Database Backend
CRM_DB_BACKEND=mysql
AIRZEN_DB_BACKEND=mysql

# MySQL Database Details (From your Coolify MySQL resource)
MYSQL_HOST=mysql-container-name-or-internal-ip
MYSQL_PORT=3306
MYSQL_DATABASE=smiledental_crm
MYSQL_USER=crm_user
MYSQL_PASSWORD=your_mysql_secure_password

# Authentication
AUTH_ENABLED=true
ADMIN_USERNAME=admin
ADMIN_PASSWORD=SmileDental@2026
ADMIN_API_KEY=smiledental-secret-key-2026

# Smart WhatsApp Follow-Up Engine
FOLLOWUP_ENABLED=true
FOLLOWUP_TIMEZONE=Asia/Kolkata
FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS=24
FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS=3

# Voice Calling (Enable when SIP Trunk is connected)
OUTBOUND_CALLING_ENABLED=false
ENABLE_VOICE_AGENT=false
VOICE_AGENT_NAME="Smile Dental Assistant"

# AI Engine
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_TTS_VOICE=Kore
USE_GEMINI_REALTIME=true
GOOGLE_API_KEY=your_gemini_api_key_here
```

### Step 4: Configure Domain & SSL
1. In Coolify, go to the application settings.
2. Under **Domains**, enter your desired domain (e.g. `https://crm.smiledentalandimplantcentre.com` or `https://smiledental.yourdomain.com`).
3. Point your DNS `A` or `CNAME` record to your Coolify VPS IP address.
4. Coolify will automatically provision a valid **Let's Encrypt SSL** certificate.
5. Click **Deploy**.

---

## 6. What Was Customized for Smile Dental

1. **Dental Pipeline Presets:**
   - `New Patient Enquiry` -> `Symptoms Evaluated` -> `Consultation Scheduled` -> `Token / Slot Confirmed` -> `Doctor Consultation Done` -> `Treatment Plan In Progress` -> `Treatment Completed` -> `Follow-up Due` -> `No Show / Cancelled`.
2. **Staff Doctors Pre-Configured:**
   - Dr. P. Sudhakar (Chief Implantologist) & Dr. K. Mounika (Cosmetic Dentist).
3. **Appointment Rules:**
   - Delivery Mode: `in_clinic`.
   - Timings: Mon-Sat `09:00 - 21:00`, Sun `10:00 - 13:00`.
   - Intake prompt asking for patient name, age, symptom, and doctor preference.
4. **Knowledge Base:**
   - High-fidelity JSON containing treatment descriptions, pricing indications, Google Maps directions, landmark details, and answers to all patient FAQs.
5. **AI Assistant Instructions:**
   - Empathic, professional, reassuring dental tone emphasizing painless modern dentistry and encouraging clinic consultation.

---

## 7. Dental Billing, Revenue Tracking & Sales Reporting System

A dedicated clinic financial, invoicing, and revenue tracking system built specifically for Smile Dental:

1. **Interactive Invoicing & Treatment Catalog:**
   - Multi-item dental bill generator with pre-loaded Smile Dental procedures (Consultation, Scaling & Polishing, Composite Fillings, Painless RCT, Zirconia Crowns, Titanium Dental Implants, All-on-4, Clear Aligners, Dentures, etc.).
   - Tooth number recording per procedure (e.g. Tooth #14, #21).
   - Automated subtotal, discount, tax, and balance calculations.
   - Sequential clinic invoice numbering: `SD-YYYY-XXXX`.

2. **Partial Payments & Installment Management:**
   - Multi-channel payments: UPI (GPay, PhonePe), Cash, Debit/Credit Card (POS), Bank Transfer, and Bajaj Finserv Dental EMI.
   - Real-time tracking of `Paid in Full`, `Partial / Balance Due`, and `Pending Payment`.
   - Installment recording for multi-sitting treatments (e.g., Implants, Root Canals, Smile Makeovers).

3. **Doctor Revenue Attribution:**
   - Attending doctor attribution on each invoice (Dr. P. Sudhakar vs Dr. K. Mounika).
   - Real-time doctor revenue share breakdown, realization percentage, and case counts.

4. **Sales Analytics & Reports:**
   - Real-time KPIs: Total Billed, Total Collected, Total Outstanding, Average Bill Value.
   - Filter by date range, attending doctor, and payment status.
   - 1-Click Excel / CSV sales report export (`/api/billing/export`).

5. **Printable Clinic Tax Bill & 1-Click WhatsApp Receipts:**
   - Professional, clinic-branded printable invoice with clinic address, phone numbers, treatment line items, and authorized doctor signature line.
   - 1-Click WhatsApp itemized receipt generator dispatched directly to patient's mobile number.

