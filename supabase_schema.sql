-- ═══════════════════════════════════════════════════════
-- OutboundAI — Complete Database Schema
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    outcome TEXT,
    reason TEXT,
    duration_seconds INTEGER,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);

ALTER TABLE appointments  DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs     DISABLE ROW LEVEL SECURITY;
ALTER TABLE settings      DISABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs    DISABLE ROW LEVEL SECURITY;

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_url TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_object_key text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_size_bytes bigint DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_deleted boolean DEFAULT false;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_deleted_at timestamptz;

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);
ALTER TABLE campaigns DISABLE ROW LEVEL SECURITY;

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calcom_booking_uid TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS completed_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS no_show_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS rescheduled_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS rescheduled_from text;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS notes text;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_id TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_name TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 30;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS buffer_minutes INTEGER NOT NULL DEFAULT 15;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Singapore';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS confirmation_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS confirmation_sent_at TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_notified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS telegram_notified BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS notification_error TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS customer_reminder_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS customer_reminder_sent_at TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_reminder_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_reminder_sent_at TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS telegram_reminder_sent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS telegram_reminder_sent_at TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_error TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_processed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_processed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS meet_link TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS google_meet_link TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendar_event_id TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS service_address text NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS location_latitude double precision;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS location_longitude double precision;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS location_name text NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS location_url text NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS intake_data jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_notification_message_id text NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_notification_sent_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS service_started_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_status_updated_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_status_updated_by text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_appointments_staff ON appointments(staff_id);
CREATE INDEX IF NOT EXISTS idx_appointments_date_time ON appointments(date, time);
CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);

CREATE TABLE IF NOT EXISTS appointment_staff (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    whatsapp_number TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'sales',
    calendar_email TEXT NOT NULL DEFAULT '',
    google_calendar_id TEXT NOT NULL DEFAULT '',
    google_calendar_connected BOOLEAN NOT NULL DEFAULT false,
    google_meet_enabled BOOLEAN NOT NULL DEFAULT false,
    calendar_sync_error TEXT NOT NULL DEFAULT '',
    last_calendar_sync_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    working_days TEXT NOT NULL DEFAULT '["mon","tue","wed","thu","fri","sat"]',
    start_time TEXT NOT NULL DEFAULT '09:00',
    end_time TEXT NOT NULL DEFAULT '18:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Singapore',
    active BOOLEAN NOT NULL DEFAULT true,
    round_robin_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE appointment_staff DISABLE ROW LEVEL SECURITY;
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'sales';
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS google_calendar_id TEXT NOT NULL DEFAULT '';
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS google_calendar_connected BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS google_meet_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS calendar_sync_error TEXT NOT NULL DEFAULT '';
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS last_calendar_sync_at TEXT;
ALTER TABLE appointment_staff ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_appointment_staff_active ON appointment_staff(active);
CREATE INDEX IF NOT EXISTS idx_appointment_staff_order ON appointment_staff(round_robin_order);

CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE contact_memory DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_contact_memory_phone ON contact_memory (phone_number);

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS agent_profile_id TEXT;

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT 'Aoede',
    model TEXT NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE agent_profiles DISABLE ROW LEVEL SECURITY;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS lead_statuses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text UNIQUE NOT NULL,
    color text,
    created_at timestamptz DEFAULT now()
);
ALTER TABLE lead_statuses DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS crm_contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number text UNIQUE NOT NULL,
    lead_name text,
    address text,
    crm_status text DEFAULT 'New Lead',
    custom_status text,
    next_followup_at timestamptz,
    assigned_to text,
    crm_notes text,
    last_call_outcome text,
    last_call_at timestamptz,
    total_calls int DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
ALTER TABLE crm_contacts DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_crm_contacts_status ON crm_contacts (crm_status);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_followup ON crm_contacts (next_followup_at);

ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS city text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS location text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS address text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS requirement text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS budget text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS source text DEFAULT 'manual';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS business_name text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS campaign_name text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS service_type text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS lead_country text DEFAULT 'Singapore';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS lead_country_code text DEFAULT '+65';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS upload_batch_id text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS import_source text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS journey_stage text DEFAULT 'new_lead';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS next_best_action text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS next_action_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS next_action_channel text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_customer_reply_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_whatsapp_sent_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_call_attempt_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS call_attempt_count integer DEFAULT 0;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS whatsapp_followup_count integer DEFAULT 0;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS no_response_followup_count integer DEFAULT 0;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS demo_reminder_count integer DEFAULT 0;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS stop_automation boolean DEFAULT false;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS stop_automation_reason text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_followup_reason text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_intent text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS preferred_channel text DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS preferred_callback_at timestamptz;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS tags_json text NOT NULL DEFAULT '[]';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS custom_fields_json text NOT NULL DEFAULT '{}';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS handoff_required boolean NOT NULL DEFAULT false;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS handoff_reason text NOT NULL DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS handoff_assigned_to text NOT NULL DEFAULT '';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS handoff_at timestamptz;
CREATE INDEX IF NOT EXISTS idx_crm_contacts_handoff ON crm_contacts(handoff_required);

CREATE TABLE IF NOT EXISTS crm_tags (
    name text PRIMARY KEY,
    color text DEFAULT '#6366f1',
    created_at timestamptz DEFAULT now()
);
ALTER TABLE crm_tags DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS crm_custom_fields (
    key text PRIMARY KEY,
    label text NOT NULL,
    field_type text NOT NULL DEFAULT 'text',
    created_at timestamptz DEFAULT now()
);
ALTER TABLE crm_custom_fields DISABLE ROW LEVEL SECURITY;

INSERT INTO crm_custom_fields (key, label, field_type) VALUES
('budget', 'Budget', 'text'),
('requirement_type', 'Requirement Type', 'text'),
('priority', 'Priority', 'text'),
('location', 'Location', 'text'),
('campaign', 'Campaign', 'text'),
('product_service_interest', 'Product/Service Interest', 'text')
ON CONFLICT (key) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_crm_contacts_source ON crm_contacts(source);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_business_name ON crm_contacts(business_name);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_campaign_name ON crm_contacts(campaign_name);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_city ON crm_contacts(city);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_created_at ON crm_contacts(created_at);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_journey_stage ON crm_contacts(journey_stage);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_next_action ON crm_contacts(next_action_at);

CREATE TABLE IF NOT EXISTS broadcast_campaigns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    template_purpose text NOT NULL DEFAULT '',
    template_name text NOT NULL DEFAULT '',
    segment jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'draft',
    scheduled_at timestamptz,
    sent_count integer NOT NULL DEFAULT 0,
    failed_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE broadcast_campaigns DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_status ON broadcast_campaigns(status);
CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_scheduled ON broadcast_campaigns(scheduled_at);

CREATE TABLE IF NOT EXISTS broadcast_recipients (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL,
    phone_number text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    provider_message_id text NOT NULL DEFAULT '',
    error_message text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE broadcast_recipients DISABLE ROW LEVEL SECURITY;
CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_recipients_unique ON broadcast_recipients(campaign_id, phone_number);
CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_campaign ON broadcast_recipients(campaign_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status ON broadcast_recipients(status);

CREATE TABLE IF NOT EXISTS followup_actions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number text NOT NULL,
    lead_name text,
    event_type text NOT NULL,
    action_type text NOT NULL,
    channel text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    status text DEFAULT 'scheduled',
    priority integer DEFAULT 5,
    source text DEFAULT '',
    source_id text DEFAULT '',
    reason text DEFAULT '',
    payload jsonb DEFAULT '{}'::jsonb,
    result jsonb DEFAULT '{}'::jsonb,
    error_message text DEFAULT '',
    attempt_number integer DEFAULT 0,
    max_attempts integer DEFAULT 3,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    completed_at timestamptz
);
ALTER TABLE followup_actions DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_followup_actions_status_scheduled ON followup_actions(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_followup_actions_phone ON followup_actions(phone_number);
CREATE INDEX IF NOT EXISTS idx_followup_actions_event_type ON followup_actions(event_type);
CREATE INDEX IF NOT EXISTS idx_followup_actions_channel ON followup_actions(channel);

INSERT INTO lead_statuses (name, color) VALUES
('New Lead', '#64748b'),
('callback_requested', '#f59e0b'),
('message_followup_requested', '#06b6d4'),
('whatsapp_no_response', '#94a3b8'),
('first_call_no_answer', '#f97316'),
('first_call_busy', '#f97316'),
('demo_booked', '#3b82f6'),
('demo_reminder_due', '#6366f1'),
('demo_no_response', '#a855f7'),
('demo_no_show', '#dc2626'),
('demo_reschedule_requested', '#8b5cf6'),
('not_interested', '#dc2626'),
('wrong_number', '#9ca3af'),
('do_not_contact', '#111827'),
('converted', '#22c55e'),
('lost', '#6b7280')
ON CONFLICT (name) DO NOTHING;

INSERT INTO settings (key, value, updated_at) VALUES
('APP_COUNTRY', 'Singapore', now()::text),
('APP_COUNTRY_CODE', '+65', now()::text),
('PHONE_FORMAT', '+65XXXXXXXX', now()::text),
('APP_TIMEZONE', 'Asia/Singapore', now()::text),
('APPOINTMENT_TIMEZONE', 'Asia/Singapore', now()::text),
('WHATSAPP_DISPLAY_TIMEZONE', 'Asia/Singapore', now()::text),
('FOLLOWUP_ENABLED', 'true', now()::text),
('FOLLOWUP_TIMEZONE', 'Asia/Singapore', now()::text),
('FOLLOWUP_MAX_CALLS_PER_DAY', '2', now()::text),
('FOLLOWUP_MAX_CALL_ATTEMPTS_TOTAL', '3', now()::text),
('FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS', '3', now()::text),
('FOLLOWUP_WELCOME_NO_RESPONSE_CALL_DELAY_MINUTES', '30', now()::text),
('FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS', '24', now()::text),
('FOLLOWUP_BUSY_RETRY_DELAY_HOURS', '2', now()::text),
('FOLLOWUP_DEMO_REMINDER_24H', 'true', now()::text),
('FOLLOWUP_DEMO_REMINDER_2H', 'true', now()::text),
('FOLLOWUP_DEMO_REMINDER_15M', 'true', now()::text),
('FOLLOWUP_STOP_ON_NOT_INTERESTED', 'true', now()::text),
('FOLLOWUP_STOP_ON_WRONG_NUMBER', 'true', now()::text),
('APPOINTMENT_TELEGRAM_REMINDER_ENABLED', '0', now()::text),
('staff_appointment_notification_template', 'staff_appointment_notification', now()::text),
('staff_appointment_reminder_template', 'staff_appointment_reminder', now()::text),
('staff_handoff_notification_template', 'staff_appointment_alert', now()::text),
('GOOGLE_CALENDAR_ENABLED', 'false', now()::text),
('GOOGLE_MEET_ENABLED', 'false', now()::text),
('GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON', '', now()::text),
('GOOGLE_CALENDAR_DEFAULT_TIMEZONE', 'Asia/Singapore', now()::text),
('GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL', 'true', now()::text),
('AUTH_ENABLED', 'false', now()::text),
('ADMIN_API_KEY', '', now()::text),
('WEBHOOK_VERIFY_TOKEN', '', now()::text),
('COST_GEMINI_VOICE_PER_MINUTE', '0', now()::text),
('COST_SIP_PER_MINUTE', '0', now()::text),
('COST_RECORDING_PER_MINUTE', '0', now()::text),
('COST_WHATSAPP_TEMPLATE', '0', now()::text),
('COST_WHATSAPP_FREE_TEXT', '0', now()::text),
('COST_CURRENCY', 'SGD', now()::text),
('BROADCAST_MAX_SEND_PER_RUN', '50', now()::text),
('KNOWLEDGE_BASE_JSON', $json${"raw_markdown":"# AIRZEN KNOWLEDGE BASE (WhatsApp AI)\n\n# COMPANY INFORMATION\n\nBusiness Name:\nAirZen Pte. Ltd.\n\nBrand Name:\nAirZen\n\nTagline:\nClean Air. Peace of Mind.\n\nBusiness Description:\nAirZen is a professional residential air conditioning service company in Singapore...","company_profile":{"business_name":"AirZen","legal_name":"AirZen Pte. Ltd.","short_description":"Clean Air. Peace of Mind.","about_us":"AirZen is a professional residential air conditioning service company in Singapore.","industry_type":"Residential air conditioning services","owner_name":"","website":"","email":"","phone":"","whatsapp_number":""},"contact_details":{"primary_phone":"","support_phone":"","whatsapp_number":"","email":"","website":"","address":"","city":"Singapore","state":"","country":"Singapore","google_maps_link":""},"working_hours":{"timezone":"Asia/Singapore","opening_days":["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"],"opening_time":"09:00","closing_time":"18:00","holiday_notes":"","emergency_support_available":false},"locations":[],"services":[],"packages":[],"faqs":[{"question":"What is AirZen?","answer":"AirZen is a professional residential air conditioning service company in Singapore.","category":"General","language":"English","active":true},{"question":"What is AirZen's tagline?","answer":"Clean Air. Peace of Mind.","category":"General","language":"English","active":true}],"policies":{"cancellation_policy":"","refund_policy":"","privacy_policy":"","appointment_policy":"","payment_policy":"","terms_notes":""},"appointment_rules":{"appointment_required":true,"allow_same_day_booking":true,"appointment_duration_minutes":30,"appointment_buffer_minutes":15,"default_visit_type":"phone_consultation","confirmation_required":true,"reminder_before_hours":24},"transfer_rules":{"transfer_enabled":false,"transfer_number":"","transfer_conditions":"","working_hours_only":true}}$json$, now()::text),
('AI_PROMPT_voice_call', $prompt$IMPORTANT: The system may already speak a fixed greeting before this prompt runs.
Do not generate a separate opening greeting or say hello again.

You are Priya from AirZen.
Use KNOWLEDGE_BASE_JSON as the single source of truth for AirZen.
Answer questions about AirZen, air conditioning services, pricing, policies, location, working hours, and appointments only from the knowledge base.
If information is not in the knowledge base, say the AirZen team will confirm and get back.
Keep replies short, warm, professional, and ask one question at a time.
If the customer is interested, collect their preferred appointment date and time.
$prompt$, now()::text),
('AI_PROMPT_whatsapp_chat', $prompt$You are Priya, a WhatsApp chat assistant for AirZen.
Reply in short, clear, friendly WhatsApp messages.
Use KNOWLEDGE_BASE_JSON as the single source of truth for AirZen.
Answer questions about AirZen, air conditioning services, pricing, policies, location, working hours, and appointments only from the knowledge base.
If information is not in the knowledge base, say the AirZen team will confirm and get back.
If the customer wants to book, collect name, service, preferred date, and preferred time.
Do not invent prices, services, locations, policies, guarantees, or timelines.
$prompt$, now()::text),
('OUTBOUND_FIXED_GREETING', 'Hi, this is Priya calling from AirZen. Is this a good time to speak for a minute?', now()::text)
ON CONFLICT (key) DO NOTHING;

-- ── Phase 8: WhatsApp Chat Inbox ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    crm_contact_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    ai_enabled BOOLEAN NOT NULL DEFAULT true,
    assigned_to TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    last_message_at TEXT NOT NULL DEFAULT '',
    unread_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'whatsapp',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE whatsapp_conversations DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_wa_conv_phone ON whatsapp_conversations(phone_number);
CREATE INDEX IF NOT EXISTS idx_wa_conv_last_msg ON whatsapp_conversations(last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_wa_conv_status ON whatsapp_conversations(status);
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS deleted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS deleted_by TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS appointment_state TEXT NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_wa_conv_is_deleted ON whatsapp_conversations(is_deleted);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'text',
    message_text TEXT NOT NULL DEFAULT '',
    template_name TEXT NOT NULL DEFAULT '',
    media_url TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    provider_status TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '{}',
    ai_generated BOOLEAN NOT NULL DEFAULT false,
    human_sent BOOLEAN NOT NULL DEFAULT false,
    created_at TEXT NOT NULL
);
ALTER TABLE whatsapp_messages DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_wa_msg_conv ON whatsapp_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_wa_msg_phone ON whatsapp_messages(phone_number);
CREATE INDEX IF NOT EXISTS idx_wa_msg_created ON whatsapp_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wa_msg_provider_id ON whatsapp_messages(provider_message_id);
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS delivered_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS read_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS failed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS media_id TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS mime_type TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS file_name TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS caption TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS deleted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS deleted_by TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_wa_msg_is_deleted ON whatsapp_messages(is_deleted);

-- ── Phase 7: Automation Actions Queue ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_actions (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
ALTER TABLE automation_actions DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_automation_actions_status ON automation_actions(status);
CREATE INDEX IF NOT EXISTS idx_automation_actions_phone ON automation_actions(phone_number);
CREATE INDEX IF NOT EXISTS idx_automation_actions_scheduled_at ON automation_actions(scheduled_at);
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS cooldown_until TEXT NOT NULL DEFAULT '';
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS action_status TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_automation_actions_idempotency ON automation_actions(idempotency_key);

-- ── Phase 7: WhatsApp Logs ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS whatsapp_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    template_name TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'en',
    parameters TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'unknown',
    provider_message_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
ALTER TABLE whatsapp_logs DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_phone ON whatsapp_logs(phone_number);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_status ON whatsapp_logs(status);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_created_at ON whatsapp_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_provider_id ON whatsapp_logs(provider_message_id);
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS delivered_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS read_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS failed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS cooldown_until TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_idempotency ON whatsapp_logs(idempotency_key);

-- ── Phase 11: Appointment Demo Completion Follow-up ─────────────────────────
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_requested boolean DEFAULT false;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_requested_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_request_count integer DEFAULT 0;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_updated_by text;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_updated_at timestamptz;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_result text;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_result_notes text;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS demo_status_reply_message_id text;

-- Phase 3 repository worker ownership and lease fields. Supabase remains the
-- production backend; these definitions keep its repository behavior aligned
-- with the MySQL staging implementation during migration validation.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS updated_at TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_campaigns_claim ON campaigns(claim_token, lease_expires_at);

ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 5;
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ;
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS updated_at TEXT;
CREATE INDEX IF NOT EXISTS idx_automation_actions_claim ON automation_actions(claim_token, lease_expires_at);

ALTER TABLE broadcast_campaigns ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE broadcast_campaigns ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_broadcast_campaigns_claim ON broadcast_campaigns(claim_token, lease_expires_at);

ALTER TABLE broadcast_recipients ADD COLUMN IF NOT EXISTS claim_token TEXT;
ALTER TABLE broadcast_recipients ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_claim ON broadcast_recipients(claim_token);
CREATE UNIQUE INDEX IF NOT EXISTS uq_broadcast_recipient ON broadcast_recipients(campaign_id, phone_number);

CREATE OR REPLACE FUNCTION set_default_agent_profile_atomic(target_profile_id TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('airzen_agent_profile_default'));
    IF NOT EXISTS (SELECT 1 FROM agent_profiles WHERE id = target_profile_id FOR UPDATE) THEN
        RETURN FALSE;
    END IF;
    UPDATE agent_profiles SET is_default = FALSE WHERE is_default = TRUE;
    UPDATE agent_profiles SET is_default = TRUE WHERE id = target_profile_id;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION claim_campaign_run(target_campaign_id TEXT, lease_seconds INTEGER DEFAULT 3600)
RETURNS SETOF campaigns
LANGUAGE plpgsql
AS $$
DECLARE
    v_claimed_token TEXT := gen_random_uuid()::TEXT;
BEGIN
    RETURN QUERY
    UPDATE campaigns
       SET status = 'running',
           claim_token = v_claimed_token,
           lease_expires_at = clock_timestamp() + make_interval(secs => GREATEST(30, LEAST(lease_seconds, 86400))),
           updated_at = clock_timestamp()::TEXT
     WHERE id = target_campaign_id
       AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
       AND (
           status IN ('active', 'scheduled')
           OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()))
       )
    RETURNING *;
END;
$$;

CREATE OR REPLACE FUNCTION claim_automation_actions(due_before TEXT, batch_limit INTEGER DEFAULT 20, lease_seconds INTEGER DEFAULT 300)
RETURNS SETOF automation_actions
LANGUAGE plpgsql
AS $$
DECLARE
    v_claimed_token TEXT := gen_random_uuid()::TEXT;
BEGIN
    RETURN QUERY
    WITH candidates AS (
        SELECT id
          FROM automation_actions
         WHERE scheduled_at::TIMESTAMPTZ <= due_before::TIMESTAMPTZ
           AND attempt_count < max_attempts
           AND (
               status IN ('pending', 'waiting', 'waiting_schedule')
               OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()))
           )
         ORDER BY scheduled_at
         LIMIT GREATEST(1, LEAST(batch_limit, 100))
         FOR UPDATE SKIP LOCKED
    )
    UPDATE automation_actions AS action
       SET status = 'running',
           action_status = 'running',
           attempt_count = action.attempt_count + 1,
           claim_token = v_claimed_token,
           lease_expires_at = clock_timestamp() + make_interval(secs => GREATEST(30, LEAST(lease_seconds, 3600))),
           processing_started_at = clock_timestamp(),
           updated_at = clock_timestamp()::TEXT
      FROM candidates
     WHERE action.id = candidates.id
    RETURNING action.*;
END;
$$;

CREATE OR REPLACE FUNCTION claim_broadcast_campaign(target_campaign_id TEXT, lease_seconds INTEGER DEFAULT 1800)
RETURNS SETOF broadcast_campaigns
LANGUAGE plpgsql
AS $$
DECLARE
    v_claimed_token TEXT := gen_random_uuid()::TEXT;
BEGIN
    RETURN QUERY
    UPDATE broadcast_campaigns
       SET status = 'running',
           claim_token = v_claimed_token,
           lease_expires_at = clock_timestamp() + make_interval(secs => GREATEST(30, LEAST(lease_seconds, 86400))),
           updated_at = clock_timestamp()::TEXT
     WHERE id = target_campaign_id
       AND (claim_token IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
       AND (
           status IN ('draft', 'scheduled')
           OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp()))
       )
    RETURNING *;
END;
$$;

CREATE OR REPLACE FUNCTION airzen_schema_health()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
DECLARE
    item RECORD;
    missing_tables TEXT[] := ARRAY[]::TEXT[];
    missing_columns JSONB := '{}'::JSONB;
    missing_indexes JSONB := '{}'::JSONB;
    table_missing_columns TEXT[];
    table_missing_indexes TEXT[];
BEGIN
    FOR item IN
        SELECT * FROM (VALUES
            ('crm_contacts'), ('contact_memory'), ('whatsapp_conversations'), ('whatsapp_messages'), ('whatsapp_logs'),
            ('appointments'), ('appointment_staff'), ('followup_actions'), ('notification_outbox'), ('settings'),
            ('error_logs'), ('lead_statuses'), ('crm_tags'), ('call_logs'), ('agent_profiles'), ('campaigns'),
            ('automation_actions'), ('broadcast_campaigns'), ('broadcast_recipients'), ('whatsapp_test_recipients'),
            ('whatsapp_health_test_runs'), ('whatsapp_health_test_results')
        ) AS required(table_name)
    LOOP
        IF to_regclass('public.' || item.table_name) IS NULL THEN
            missing_tables := array_append(missing_tables, item.table_name);
        END IF;
    END LOOP;

    FOR item IN
        SELECT table_name, array_agg(column_name) AS columns
        FROM (VALUES
            ('crm_contacts','id'), ('crm_contacts','phone_number'),
            ('whatsapp_conversations','appointment_state'), ('whatsapp_messages','provider_message_id'),
            ('notification_outbox','claim_token'), ('appointments','rescheduled_from'),
            ('call_logs','recording_object_key'), ('agent_profiles','is_default'),
            ('campaigns','claim_token'), ('campaigns','lease_expires_at'),
            ('automation_actions','claim_token'), ('automation_actions','lease_expires_at'),
            ('broadcast_campaigns','claim_token'), ('broadcast_campaigns','lease_expires_at'),
            ('broadcast_recipients','claim_token'), ('whatsapp_test_recipients','phone_number_fingerprint'),
            ('whatsapp_health_test_runs','selected_tests'), ('whatsapp_health_test_results','test_key')
        ) AS required(table_name, column_name)
        GROUP BY table_name
    LOOP
        SELECT COALESCE(array_agg(required_column.column_name), ARRAY[]::TEXT[])
          INTO table_missing_columns
          FROM unnest(item.columns) AS required_column(column_name)
         WHERE NOT EXISTS (
             SELECT 1 FROM information_schema.columns current_column
              WHERE current_column.table_schema = 'public'
                AND current_column.table_name = item.table_name
                AND current_column.column_name = required_column.column_name
         );
        IF cardinality(table_missing_columns) > 0 THEN
            missing_columns := missing_columns || jsonb_build_object(item.table_name, to_jsonb(table_missing_columns));
        END IF;
    END LOOP;

    FOR item IN
        SELECT table_name, array_agg(index_name) AS indexes
        FROM (VALUES
            ('crm_contacts','crm_contacts_phone_number_key'),
            ('lead_statuses','lead_statuses_name_key'),
            ('notification_outbox','notification_outbox_dedupe_key_key'),
            ('broadcast_recipients','uq_broadcast_recipient'),
            ('campaigns','idx_campaigns_claim'),
            ('automation_actions','idx_automation_actions_claim'),
            ('broadcast_campaigns','idx_broadcast_campaigns_claim'),
            ('broadcast_recipients','idx_broadcast_recipients_claim'),
            ('whatsapp_test_recipients','whatsapp_test_recipients_phone_number_fingerprint_key'),
            ('whatsapp_health_test_runs','idx_whatsapp_health_runs_recipient_started'),
            ('whatsapp_health_test_results','idx_whatsapp_health_results_run'),
            ('whatsapp_health_test_results','whatsapp_health_test_results_test_run_id_test_key_key')
        ) AS required(table_name, index_name)
        GROUP BY table_name
    LOOP
        SELECT COALESCE(array_agg(required_index.index_name), ARRAY[]::TEXT[])
          INTO table_missing_indexes
          FROM unnest(item.indexes) AS required_index(index_name)
         WHERE NOT EXISTS (
             SELECT 1 FROM pg_indexes current_index
              WHERE current_index.schemaname = 'public'
                AND current_index.tablename = item.table_name
                AND current_index.indexname = required_index.index_name
         );
        IF cardinality(table_missing_indexes) > 0 THEN
            missing_indexes := missing_indexes || jsonb_build_object(item.table_name, to_jsonb(table_missing_indexes));
        END IF;
    END LOOP;

    RETURN jsonb_build_object(
        'ok', cardinality(missing_tables) = 0 AND missing_columns = '{}'::JSONB AND missing_indexes = '{}'::JSONB,
        'validation_mode', 'catalog_manifest',
        'required_tables_missing', to_jsonb(missing_tables),
        'required_columns_missing', missing_columns,
        'critical_constraints_missing', '{}'::JSONB,
        'critical_indexes_missing', missing_indexes
    );
END;
$$;

REVOKE ALL ON FUNCTION airzen_schema_health() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION airzen_schema_health() TO service_role;
