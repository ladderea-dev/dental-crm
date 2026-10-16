ALTER TABLE crm_contacts
    ADD COLUMN IF NOT EXISTS lead_country text DEFAULT 'Singapore',
    ADD COLUMN IF NOT EXISTS lead_country_code text DEFAULT '+65';

ALTER TABLE crm_contacts
    ALTER COLUMN crm_status SET DEFAULT 'New Lead';

UPDATE crm_contacts
SET
    lead_country = COALESCE(NULLIF(lead_country, ''), 'Singapore'),
    lead_country_code = COALESCE(NULLIF(lead_country_code, ''), '+65'),
    crm_status = CASE
        WHEN crm_status IS NULL OR crm_status = '' OR crm_status = 'new_lead' OR crm_status = 'New' THEN 'New Lead'
        ELSE crm_status
    END,
    source = CASE
        WHEN lower(COALESCE(source, '')) LIKE '%whatsapp%' THEN 'WhatsApp'
        WHEN lower(COALESCE(source, '')) LIKE '%voice%' OR lower(COALESCE(source, '')) LIKE '%call%' THEN 'Voice Call'
        WHEN source IS NULL OR source = '' OR lower(source) IN ('manual', 'api', 'file_upload', 'uploaded', 'upload', 'website', 'webform') THEN 'Website'
        ELSE source
    END
WHERE
    lead_country IS NULL
    OR lead_country = ''
    OR lead_country_code IS NULL
    OR lead_country_code = ''
    OR crm_status IS NULL
    OR crm_status = ''
    OR crm_status IN ('new_lead', 'New')
    OR source IS NULL
    OR source = ''
    OR lower(source) IN ('manual', 'api', 'file_upload', 'uploaded', 'upload', 'website', 'webform')
    OR lower(source) LIKE '%whatsapp%'
    OR lower(source) LIKE '%voice%'
    OR lower(source) LIKE '%call%';

INSERT INTO lead_statuses (name, color)
VALUES ('New Lead', '#64748b')
ON CONFLICT (name) DO NOTHING;

INSERT INTO settings (key, value, updated_at)
VALUES
(
    'KNOWLEDGE_BASE_JSON',
    $json$
{
  "raw_markdown": "# AIRZEN KNOWLEDGE BASE (WhatsApp AI)\n\n# COMPANY INFORMATION\n\nBusiness Name:\nAirZen Pte. Ltd.\n\nBrand Name:\nAirZen\n\nTagline:\nClean Air. Peace of Mind.\n\nBusiness Description:\nAirZen is a professional residential air conditioning service company in Singapore...",
  "company_profile": {
    "business_name": "AirZen",
    "legal_name": "AirZen Pte. Ltd.",
    "short_description": "Clean Air. Peace of Mind.",
    "about_us": "AirZen is a professional residential air conditioning service company in Singapore.",
    "industry_type": "Residential air conditioning services",
    "owner_name": "",
    "website": "",
    "email": "",
    "phone": "",
    "whatsapp_number": ""
  },
  "contact_details": {
    "primary_phone": "",
    "support_phone": "",
    "whatsapp_number": "",
    "email": "",
    "website": "",
    "address": "",
    "city": "Singapore",
    "state": "",
    "country": "Singapore",
    "google_maps_link": ""
  },
  "working_hours": {
    "timezone": "Asia/Singapore",
    "opening_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
    "opening_time": "09:00",
    "closing_time": "18:00",
    "holiday_notes": "",
    "emergency_support_available": false
  },
  "locations": [],
  "services": [],
  "packages": [],
  "faqs": [
    {
      "question": "What is AirZen?",
      "answer": "AirZen is a professional residential air conditioning service company in Singapore.",
      "category": "General",
      "language": "English",
      "active": true
    },
    {
      "question": "What is AirZen's tagline?",
      "answer": "Clean Air. Peace of Mind.",
      "category": "General",
      "language": "English",
      "active": true
    }
  ],
  "policies": {
    "cancellation_policy": "",
    "refund_policy": "",
    "privacy_policy": "",
    "appointment_policy": "",
    "payment_policy": "",
    "terms_notes": ""
  },
  "appointment_rules": {
    "appointment_required": true,
    "allow_same_day_booking": true,
    "appointment_duration_minutes": 30,
    "appointment_buffer_minutes": 15,
    "default_visit_type": "phone_consultation",
    "confirmation_required": true,
    "reminder_before_hours": 24
  },
  "transfer_rules": {
    "transfer_enabled": false,
    "transfer_number": "",
    "transfer_conditions": "",
    "working_hours_only": true
  }
}
$json$,
    now()::text
),
(
    'AI_PROMPT_voice_call',
    $prompt$IMPORTANT: The system may already speak a fixed greeting before this prompt runs.
Do not generate a separate opening greeting or say hello again.

You are Priya from AirZen.
Use KNOWLEDGE_BASE_JSON as the single source of truth for AirZen.
Answer questions about AirZen, air conditioning services, pricing, policies, location, working hours, and appointments only from the knowledge base.
If information is not in the knowledge base, say the AirZen team will confirm and get back.

Call rules:
- Keep replies short, warm, and professional.
- Ask one question at a time.
- If the customer is interested, collect their preferred appointment date and time.
- If the customer is busy, ask for a callback time.
- If the customer is not interested, politely close the call.
- If the customer asks for WhatsApp or a later message, schedule the appropriate follow-up.
Do not invent prices, services, locations, policies, guarantees, or timelines.
$prompt$,
    now()::text
),
(
    'AI_PROMPT_whatsapp_chat',
    $prompt$You are Priya, a WhatsApp chat assistant for AirZen.
Reply in short, clear, friendly WhatsApp messages.

Use KNOWLEDGE_BASE_JSON as the single source of truth for AirZen.
Answer questions about AirZen, air conditioning services, pricing, policies, location, working hours, and appointments only from the knowledge base.
If information is not in the knowledge base, say the AirZen team will confirm and get back.

If the customer wants to book, collect name, service, preferred date, and preferred time.
If the customer asks for a call, ask for a preferred time if missing.
If human takeover is needed, say: Let me connect you with our team, they will respond shortly.
Do not invent prices, services, locations, policies, guarantees, or timelines.
$prompt$,
    now()::text
),
(
    'OUTBOUND_FIXED_GREETING',
    'Hi, this is Priya calling from AirZen. Is this a good time to speak for a minute?',
    now()::text
),
('APP_COUNTRY', 'Singapore', now()::text),
('APP_COUNTRY_CODE', '+65', now()::text),
('PHONE_FORMAT', '+65XXXXXXXX', now()::text),
('APP_TIMEZONE', 'Asia/Singapore', now()::text),
('APPOINTMENT_TIMEZONE', 'Asia/Singapore', now()::text),
('WHATSAPP_DISPLAY_TIMEZONE', 'Asia/Singapore', now()::text),
('FOLLOWUP_TIMEZONE', 'Asia/Singapore', now()::text),
('GOOGLE_CALENDAR_DEFAULT_TIMEZONE', 'Asia/Singapore', now()::text),
('OUTBOUND_TIMEZONE', 'Asia/Singapore', now()::text),
('COST_CURRENCY', 'SGD', now()::text)
ON CONFLICT (key)
DO UPDATE SET
    value = EXCLUDED.value,
    updated_at = EXCLUDED.updated_at;
