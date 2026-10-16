-- AirZen MySQL 8 schema foundation.
-- Phase 1 only: supports the critical CRM + WhatsApp + booking + queue journey.
-- Do not run against production until the migration plan and staging validation are approved.

CREATE TABLE IF NOT EXISTS settings (
  `key` VARCHAR(191) PRIMARY KEY,
  value LONGTEXT NOT NULL,
  updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crm_contacts (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  lead_name VARCHAR(255) NULL,
  email VARCHAR(255) NULL,
  city VARCHAR(255) NULL,
  location VARCHAR(255) NULL,
  address TEXT NULL,
  requirement TEXT NULL,
  budget VARCHAR(255) NULL,
  source VARCHAR(191) NULL DEFAULT 'manual',
  business_name VARCHAR(255) NULL,
  campaign_name VARCHAR(255) NULL,
  service_type VARCHAR(255) NULL,
  lead_country VARCHAR(100) NULL DEFAULT 'Singapore',
  lead_country_code VARCHAR(16) NULL DEFAULT '+65',
  upload_batch_id VARCHAR(191) NULL,
  import_source VARCHAR(191) NULL,
  crm_status VARCHAR(191) NULL DEFAULT 'New Lead',
  custom_status VARCHAR(191) NULL,
  next_followup_at DATETIME(6) NULL,
  assigned_to VARCHAR(255) NULL,
  crm_notes TEXT NULL,
  last_call_outcome VARCHAR(191) NULL,
  last_call_at DATETIME(6) NULL,
  total_calls INT NOT NULL DEFAULT 0,
  last_synced_at DATETIME(6) NULL,
  journey_stage VARCHAR(191) NULL DEFAULT 'new_lead',
  next_best_action VARCHAR(255) NOT NULL DEFAULT '',
  next_action_at DATETIME(6) NULL,
  next_action_channel VARCHAR(64) NOT NULL DEFAULT '',
  last_customer_reply_at DATETIME(6) NULL,
  last_whatsapp_sent_at DATETIME(6) NULL,
  last_call_attempt_at DATETIME(6) NULL,
  call_attempt_count INT NOT NULL DEFAULT 0,
  whatsapp_followup_count INT NOT NULL DEFAULT 0,
  no_response_followup_count INT NOT NULL DEFAULT 0,
  demo_reminder_count INT NOT NULL DEFAULT 0,
  stop_automation BOOLEAN NOT NULL DEFAULT FALSE,
  stop_automation_reason TEXT NULL,
  last_followup_reason TEXT NULL,
  last_intent VARCHAR(255) NOT NULL DEFAULT '',
  preferred_channel VARCHAR(64) NOT NULL DEFAULT '',
  preferred_callback_at DATETIME(6) NULL,
  tags_json JSON NULL,
  custom_fields_json JSON NULL,
  handoff_required BOOLEAN NOT NULL DEFAULT FALSE,
  handoff_reason TEXT NULL,
  handoff_assigned_to VARCHAR(255) NOT NULL DEFAULT '',
  handoff_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_crm_contacts_phone (phone_number),
  KEY idx_crm_contacts_status (crm_status),
  KEY idx_crm_contacts_followup (next_followup_at),
  KEY idx_crm_contacts_source (source),
  KEY idx_crm_contacts_business_name (business_name),
  KEY idx_crm_contacts_campaign_name (campaign_name),
  KEY idx_crm_contacts_city (city),
  KEY idx_crm_contacts_created_at (created_at),
  KEY idx_crm_contacts_journey_stage (journey_stage),
  KEY idx_crm_contacts_next_action (next_action_at),
  KEY idx_crm_contacts_handoff (handoff_required)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS contact_memory (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  insight TEXT NOT NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_contact_memory_phone_created (phone_number, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_conversations (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  contact_name VARCHAR(255) NOT NULL DEFAULT '',
  crm_contact_id CHAR(36) NOT NULL DEFAULT '',
  status VARCHAR(32) NOT NULL DEFAULT 'open',
  ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  assigned_to VARCHAR(255) NOT NULL DEFAULT '',
  last_message VARCHAR(255) NOT NULL DEFAULT '',
  last_message_at DATETIME(6) NULL,
  unread_count INT NOT NULL DEFAULT 0,
  source VARCHAR(64) NOT NULL DEFAULT 'whatsapp',
  appointment_state JSON NULL,
  is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at DATETIME(6) NULL,
  deleted_by VARCHAR(255) NOT NULL DEFAULT '',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_wa_conv_phone (phone_number),
  KEY idx_wa_conv_phone (phone_number),
  KEY idx_wa_conv_last_msg (last_message_at),
  KEY idx_wa_conv_status (status),
  KEY idx_wa_conv_is_deleted (is_deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_messages (
  id CHAR(36) PRIMARY KEY,
  conversation_id CHAR(36) NOT NULL,
  phone_number VARCHAR(32) NOT NULL,
  direction VARCHAR(16) NOT NULL,
  message_type VARCHAR(64) NOT NULL DEFAULT 'text',
  message_text TEXT NULL,
  template_name VARCHAR(255) NOT NULL DEFAULT '',
  media_url TEXT NULL,
  provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
  provider_status VARCHAR(64) NOT NULL DEFAULT '',
  raw_payload JSON NULL,
  ai_generated BOOLEAN NOT NULL DEFAULT FALSE,
  human_sent BOOLEAN NOT NULL DEFAULT FALSE,
  delivered_at DATETIME(6) NULL,
  read_at DATETIME(6) NULL,
  failed_at DATETIME(6) NULL,
  failure_reason TEXT NULL,
  error_code VARCHAR(64) NOT NULL DEFAULT '',
  media_id VARCHAR(255) NOT NULL DEFAULT '',
  mime_type VARCHAR(255) NOT NULL DEFAULT '',
  file_name VARCHAR(255) NOT NULL DEFAULT '',
  caption TEXT NULL,
  is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
  deleted_at DATETIME(6) NULL,
  deleted_by VARCHAR(255) NOT NULL DEFAULT '',
  created_at DATETIME(6) NOT NULL,
  KEY idx_wa_msg_conv (conversation_id),
  KEY idx_wa_msg_phone (phone_number),
  KEY idx_wa_msg_created (created_at),
  KEY idx_wa_msg_provider_id (provider_message_id),
  KEY idx_wa_msg_is_deleted (is_deleted),
  CONSTRAINT fk_wa_msg_conv FOREIGN KEY (conversation_id) REFERENCES whatsapp_conversations(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_logs (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  event_type VARCHAR(191) NOT NULL DEFAULT '',
  template_name VARCHAR(255) NOT NULL DEFAULT '',
  language VARCHAR(32) NOT NULL DEFAULT 'en',
  parameters JSON NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'unknown',
  provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
  error_message TEXT NULL,
  source_type VARCHAR(191) NOT NULL DEFAULT '',
  source_id VARCHAR(255) NOT NULL DEFAULT '',
  delivered_at DATETIME(6) NULL,
  read_at DATETIME(6) NULL,
  failed_at DATETIME(6) NULL,
  failure_reason TEXT NULL,
  error_code VARCHAR(64) NOT NULL DEFAULT '',
  idempotency_key VARCHAR(512) NOT NULL DEFAULT '',
  cooldown_until DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_whatsapp_logs_phone (phone_number),
  KEY idx_whatsapp_logs_status (status),
  KEY idx_whatsapp_logs_created_at (created_at),
  KEY idx_whatsapp_logs_provider_id (provider_message_id),
  KEY idx_whatsapp_logs_idempotency (idempotency_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointment_staff (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL DEFAULT '',
  whatsapp_number VARCHAR(32) NOT NULL DEFAULT '',
  role VARCHAR(191) NOT NULL DEFAULT 'service_staff',
  calendar_email VARCHAR(255) NOT NULL DEFAULT '',
  google_calendar_id VARCHAR(255) NOT NULL DEFAULT '',
  google_calendar_connected BOOLEAN NOT NULL DEFAULT FALSE,
  google_meet_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  calendar_sync_error TEXT NULL,
  last_calendar_sync_at DATETIME(6) NULL,
  notes TEXT NULL,
  working_days JSON NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  timezone VARCHAR(100) NOT NULL DEFAULT 'Asia/Singapore',
  active BOOLEAN NOT NULL DEFAULT TRUE,
  round_robin_order INT NOT NULL DEFAULT 0,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_appointment_staff_active (active),
  KEY idx_appointment_staff_order (round_robin_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointments (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  phone VARCHAR(32) NOT NULL,
  `date` DATE NOT NULL,
  `time` TIME NOT NULL,
  service VARCHAR(255) NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'booked',
  staff_id CHAR(36) NOT NULL DEFAULT '',
  staff_name VARCHAR(255) NOT NULL DEFAULT '',
  duration_minutes INT NOT NULL DEFAULT 30,
  buffer_minutes INT NOT NULL DEFAULT 15,
  timezone VARCHAR(100) NOT NULL DEFAULT 'Asia/Singapore',
  confirmation_sent BOOLEAN NOT NULL DEFAULT FALSE,
  confirmation_sent_at DATETIME(6) NULL,
  staff_notified BOOLEAN NOT NULL DEFAULT FALSE,
  telegram_notified BOOLEAN NOT NULL DEFAULT FALSE,
  notification_error TEXT NULL,
  customer_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
  customer_reminder_sent_at DATETIME(6) NULL,
  staff_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
  staff_reminder_sent_at DATETIME(6) NULL,
  telegram_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
  telegram_reminder_sent_at DATETIME(6) NULL,
  reminder_error TEXT NULL,
  reminder_processed BOOLEAN NOT NULL DEFAULT FALSE,
  reminder_processed_at DATETIME(6) NULL,
  meet_link TEXT NULL,
  calcom_booking_uid TEXT NULL,
  google_meet_link TEXT NULL,
  calendar_event_id VARCHAR(255) NOT NULL DEFAULT '',
  calendar_sync_error TEXT NULL,
  last_calendar_sync_at DATETIME(6) NULL,
  service_address TEXT NULL,
  location_latitude DOUBLE NULL,
  location_longitude DOUBLE NULL,
  location_name TEXT NULL,
  location_url TEXT NULL,
  intake_data JSON NULL,
  staff_notification_message_id VARCHAR(255) NOT NULL DEFAULT '',
  staff_notification_sent_at DATETIME(6) NULL,
  service_started_at DATETIME(6) NULL,
  staff_status_updated_at DATETIME(6) NULL,
  staff_status_updated_by VARCHAR(255) NOT NULL DEFAULT '',
  cancelled_at DATETIME(6) NULL,
  completed_at DATETIME(6) NULL,
  no_show_at DATETIME(6) NULL,
  rescheduled_at DATETIME(6) NULL,
  rescheduled_from CHAR(36) NULL,
  notes TEXT NULL,
  demo_status_requested BOOLEAN NOT NULL DEFAULT FALSE,
  demo_status_requested_at DATETIME(6) NULL,
  demo_status_request_count INT NOT NULL DEFAULT 0,
  demo_status_updated_by VARCHAR(255) NULL,
  demo_status_updated_at DATETIME(6) NULL,
  demo_result VARCHAR(191) NULL,
  demo_result_notes TEXT NULL,
  demo_status_reply_message_id VARCHAR(255) NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_appointments_staff (staff_id),
  KEY idx_appointments_phone_date (phone, `date`, `time`),
  KEY idx_appointments_date_time (`date`, `time`),
  KEY idx_appointments_status (status),
  KEY idx_appointments_staff_status_date (staff_id, status, `date`, `time`),
  UNIQUE KEY uq_appointments_rescheduled_from (rescheduled_from)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS followup_actions (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  lead_name VARCHAR(255) NULL,
  event_type VARCHAR(191) NOT NULL,
  action_type VARCHAR(191) NOT NULL,
  channel VARCHAR(64) NOT NULL,
  scheduled_at DATETIME(6) NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'scheduled',
  priority INT NOT NULL DEFAULT 5,
  source VARCHAR(191) NOT NULL DEFAULT '',
  source_id VARCHAR(191) NOT NULL DEFAULT '',
  reason TEXT NULL,
  payload JSON NULL,
  result JSON NULL,
  error_message TEXT NULL,
  attempt_number INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 3,
  completed_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_followup_actions_status_scheduled (status, scheduled_at),
  KEY idx_followup_actions_phone (phone_number),
  KEY idx_followup_actions_event_type (event_type),
  KEY idx_followup_actions_channel (channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS notification_outbox (
  id CHAR(36) PRIMARY KEY,
  dedupe_key CHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  recipient_type VARCHAR(16) NOT NULL,
  recipient_phone VARCHAR(32) NOT NULL,
  notification_type VARCHAR(191) NOT NULL,
  template_purpose VARCHAR(191) NOT NULL,
  appointment_id CHAR(36) NOT NULL,
  conversation_id CHAR(36) NOT NULL DEFAULT '',
  contact_id CHAR(36) NOT NULL DEFAULT '',
  payload JSON NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 5,
  next_attempt_at DATETIME(6) NOT NULL,
  claim_token CHAR(36) NULL,
  lease_expires_at DATETIME(6) NULL,
  provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
  last_error TEXT NULL,
  processing_started_at DATETIME(6) NULL,
  sent_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_notification_outbox_dedupe (dedupe_key),
  KEY idx_notification_outbox_due (status, next_attempt_at, lease_expires_at),
  KEY idx_notification_outbox_appointment (appointment_id, notification_type, recipient_type),
  KEY idx_notification_outbox_claim (claim_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS error_logs (
  id CHAR(36) PRIMARY KEY,
  source VARCHAR(191) NOT NULL,
  level VARCHAR(32) NOT NULL DEFAULT 'error',
  message VARCHAR(500) NOT NULL,
  detail TEXT NULL,
  `timestamp` DATETIME(6) NOT NULL,
  KEY idx_error_logs_timestamp (`timestamp`),
  KEY idx_error_logs_level_source (level, source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS lead_statuses (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(191) NOT NULL,
  color VARCHAR(64) NULL,
  created_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_lead_statuses_name (name),
  KEY idx_lead_statuses_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS crm_tags (
  name VARCHAR(191) PRIMARY KEY,
  color VARCHAR(64) NOT NULL DEFAULT '#6366f1',
  created_at DATETIME(6) NOT NULL,
  KEY idx_crm_tags_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS call_logs (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  lead_name VARCHAR(255) NULL,
  outcome VARCHAR(191) NULL,
  reason TEXT NULL,
  duration_seconds INT NULL,
  `timestamp` DATETIME(6) NOT NULL,
  recording_url TEXT NULL,
  notes TEXT NULL,
  recording_object_key VARCHAR(512) NULL,
  recording_size_bytes BIGINT NOT NULL DEFAULT 0,
  recording_deleted BOOLEAN NOT NULL DEFAULT FALSE,
  recording_deleted_at DATETIME(6) NULL,
  KEY idx_call_logs_timestamp (`timestamp`),
  KEY idx_call_logs_phone_timestamp (phone_number, `timestamp`),
  KEY idx_call_logs_recording_cleanup (recording_deleted, `timestamp`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_profiles (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  voice VARCHAR(191) NOT NULL DEFAULT 'Kore',
  model VARCHAR(255) NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
  system_prompt LONGTEXT NULL,
  enabled_tools LONGTEXT NOT NULL,
  is_default BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL,
  KEY idx_agent_profiles_created_at (created_at),
  KEY idx_agent_profiles_default (is_default)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS campaigns (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'active',
  contacts_json JSON NOT NULL,
  schedule_type VARCHAR(64) NOT NULL DEFAULT 'once',
  schedule_time TIME NULL,
  call_delay_seconds INT NOT NULL DEFAULT 3,
  system_prompt LONGTEXT NULL,
  agent_profile_id CHAR(36) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NULL,
  last_run_at DATETIME(6) NULL,
  total_dispatched INT NOT NULL DEFAULT 0,
  total_failed INT NOT NULL DEFAULT 0,
  claim_token CHAR(36) NULL,
  lease_expires_at DATETIME(6) NULL,
  KEY idx_campaigns_status_schedule (status, schedule_time),
  KEY idx_campaigns_profile (agent_profile_id),
  KEY idx_campaigns_claim (claim_token, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS automation_actions (
  id CHAR(36) PRIMARY KEY,
  phone_number VARCHAR(32) NOT NULL,
  event_type VARCHAR(191) NOT NULL DEFAULT '',
  source VARCHAR(191) NOT NULL DEFAULT '',
  action_type VARCHAR(191) NOT NULL,
  scheduled_at DATETIME(6) NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'pending',
  action_status VARCHAR(64) NOT NULL DEFAULT 'pending',
  idempotency_key VARCHAR(512) NOT NULL DEFAULT '',
  cooldown_until DATETIME(6) NULL,
  payload JSON NULL,
  result JSON NULL,
  error_message TEXT NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 5,
  claim_token CHAR(36) NULL,
  lease_expires_at DATETIME(6) NULL,
  processing_started_at DATETIME(6) NULL,
  completed_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_automation_due (status, scheduled_at, lease_expires_at),
  KEY idx_automation_phone (phone_number),
  KEY idx_automation_idempotency (idempotency_key),
  KEY idx_automation_claim (claim_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS broadcast_campaigns (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  template_purpose VARCHAR(191) NOT NULL DEFAULT '',
  template_name VARCHAR(255) NOT NULL DEFAULT '',
  segment JSON NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'draft',
  scheduled_at DATETIME(6) NULL,
  sent_count INT NOT NULL DEFAULT 0,
  failed_count INT NOT NULL DEFAULT 0,
  claim_token CHAR(36) NULL,
  lease_expires_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_broadcast_campaigns_status_scheduled (status, scheduled_at),
  KEY idx_broadcast_campaigns_claim (claim_token, lease_expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS broadcast_recipients (
  id CHAR(36) PRIMARY KEY,
  campaign_id CHAR(36) NOT NULL,
  phone_number VARCHAR(32) NOT NULL,
  status VARCHAR(64) NOT NULL DEFAULT 'pending',
  provider_message_id VARCHAR(255) NOT NULL DEFAULT '',
  error_message TEXT NULL,
  claim_token CHAR(36) NULL,
  claimed_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_broadcast_recipient (campaign_id, phone_number),
  KEY idx_broadcast_recipients_campaign (campaign_id),
  KEY idx_broadcast_recipients_status (status),
  KEY idx_broadcast_recipients_claim (claim_token)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_test_recipients (
  id CHAR(36) PRIMARY KEY,
  recipient_name VARCHAR(100) NOT NULL,
  phone_number_encrypted TEXT NOT NULL,
  phone_number_fingerprint CHAR(64) NOT NULL,
  phone_number_masked VARCHAR(64) NOT NULL,
  country_code VARCHAR(16) NOT NULL,
  verified BOOLEAN NOT NULL DEFAULT FALSE,
  verification_code_hash VARCHAR(255) NULL,
  verification_expires_at DATETIME(6) NULL,
  verified_at DATETIME(6) NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  last_real_test_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_whatsapp_test_recipient_fingerprint (phone_number_fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_health_test_runs (
  id CHAR(36) PRIMARY KEY,
  started_at DATETIME(6) NOT NULL,
  completed_at DATETIME(6) NULL,
  status VARCHAR(32) NOT NULL,
  recipient_id CHAR(36) NULL,
  initiated_by VARCHAR(191) NOT NULL,
  selected_tests JSON NOT NULL,
  language VARCHAR(32) NOT NULL DEFAULT 'en',
  test_ai_reply BOOLEAN NOT NULL DEFAULT FALSE,
  allow_marketing BOOLEAN NOT NULL DEFAULT FALSE,
  max_real_messages INT NOT NULL DEFAULT 10,
  real_messages_submitted INT NOT NULL DEFAULT 0,
  stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
  total_tests INT NOT NULL DEFAULT 0,
  passed_tests INT NOT NULL DEFAULT 0,
  failed_tests INT NOT NULL DEFAULT 0,
  warning_tests INT NOT NULL DEFAULT 0,
  skipped_tests INT NOT NULL DEFAULT 0,
  report_json JSON NOT NULL,
  KEY idx_whatsapp_health_runs_recipient_started (recipient_id, started_at),
  KEY idx_whatsapp_health_runs_status (status),
  CONSTRAINT fk_health_run_recipient FOREIGN KEY (recipient_id) REFERENCES whatsapp_test_recipients(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS whatsapp_health_test_results (
  id CHAR(36) PRIMARY KEY,
  test_run_id CHAR(36) NOT NULL,
  test_key VARCHAR(191) NOT NULL,
  test_name VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  completed_at DATETIME(6) NULL,
  duration_ms INT NULL,
  provider_message_id VARCHAR(255) NULL,
  http_status INT NULL,
  provider_error_code VARCHAR(191) NULL,
  provider_error_type VARCHAR(191) NULL,
  provider_error_message TEXT NULL,
  webhook_status VARCHAR(64) NULL,
  details_json JSON NOT NULL,
  template_name VARCHAR(255) NULL,
  template_category VARCHAR(64) NULL,
  template_language VARCHAR(32) NULL,
  payload_json JSON NULL,
  provider_response_json JSON NULL,
  retry_count INT NOT NULL DEFAULT 0,
  skip_reason TEXT NULL,
  is_test BOOLEAN NOT NULL DEFAULT TRUE,
  submitted_at DATETIME(6) NULL,
  sent_at DATETIME(6) NULL,
  delivered_at DATETIME(6) NULL,
  UNIQUE KEY uq_health_result_run_key (test_run_id, test_key),
  KEY idx_whatsapp_health_results_run_started (test_run_id, started_at),
  KEY idx_whatsapp_health_results_provider (provider_message_id),
  CONSTRAINT fk_health_result_run FOREIGN KEY (test_run_id) REFERENCES whatsapp_health_test_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Idempotent upgrades for staging databases created before these appointment
-- fields were added to the canonical table definition. MySQL 8.4 does not
-- support ADD COLUMN IF NOT EXISTS, so each ALTER is selected only when the
-- column is absent.
SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA=DATABASE()
      AND TABLE_NAME='notification_outbox'
      AND COLUMN_NAME='dedupe_key'
      AND (LOWER(COLUMN_TYPE) <> 'char(64)' OR IS_NULLABLE <> 'NO')) > 0,
  'ALTER TABLE notification_outbox MODIFY COLUMN dedupe_key CHAR(64) NOT NULL',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='telegram_notified') = 0,
  'ALTER TABLE appointments ADD COLUMN telegram_notified BOOLEAN NOT NULL DEFAULT FALSE AFTER staff_notified',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='telegram_reminder_sent') = 0,
  'ALTER TABLE appointments ADD COLUMN telegram_reminder_sent BOOLEAN NOT NULL DEFAULT FALSE AFTER staff_reminder_sent_at',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='telegram_reminder_sent_at') = 0,
  'ALTER TABLE appointments ADD COLUMN telegram_reminder_sent_at DATETIME(6) NULL AFTER telegram_reminder_sent',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='calcom_booking_uid') = 0,
  'ALTER TABLE appointments ADD COLUMN calcom_booking_uid TEXT NULL AFTER meet_link',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='calendar_sync_error') = 0,
  'ALTER TABLE appointments ADD COLUMN calendar_sync_error TEXT NULL AFTER calendar_event_id',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

SET @airzen_schema_sql = IF(
  (SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='appointments' AND COLUMN_NAME='last_calendar_sync_at') = 0,
  'ALTER TABLE appointments ADD COLUMN last_calendar_sync_at DATETIME(6) NULL AFTER calendar_sync_error',
  'SELECT 1'
);
PREPARE airzen_schema_stmt FROM @airzen_schema_sql;
EXECUTE airzen_schema_stmt;
DEALLOCATE PREPARE airzen_schema_stmt;

-- ==============================================================================
-- Dental Clinic Invoicing, Billing & Revenue System
-- ==============================================================================

CREATE TABLE IF NOT EXISTS invoices (
  id CHAR(36) PRIMARY KEY,
  invoice_number VARCHAR(64) NOT NULL UNIQUE,
  patient_name VARCHAR(255) NOT NULL,
  phone VARCHAR(32) NOT NULL,
  doctor_id VARCHAR(64) NOT NULL DEFAULT '',
  doctor_name VARCHAR(255) NOT NULL DEFAULT '',
  treatment_date DATE NOT NULL,
  subtotal DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  discount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  tax DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  total_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  paid_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  balance_amount DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  payment_status VARCHAR(32) NOT NULL DEFAULT 'paid',
  payment_method VARCHAR(64) NOT NULL DEFAULT 'UPI',
  notes TEXT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  KEY idx_invoices_phone (phone),
  KEY idx_invoices_treatment_date (treatment_date),
  KEY idx_invoices_payment_status (payment_status),
  KEY idx_invoices_doctor (doctor_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS invoice_items (
  id CHAR(36) PRIMARY KEY,
  invoice_id CHAR(36) NOT NULL,
  treatment_name VARCHAR(255) NOT NULL,
  tooth_number VARCHAR(32) NOT NULL DEFAULT '',
  quantity INT NOT NULL DEFAULT 1,
  unit_price DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  total_price DECIMAL(12,2) NOT NULL DEFAULT 0.00,
  created_at DATETIME(6) NOT NULL,
  KEY idx_invoice_items_invoice_id (invoice_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS invoice_payments (
  id CHAR(36) PRIMARY KEY,
  invoice_id CHAR(36) NOT NULL,
  amount DECIMAL(12,2) NOT NULL,
  payment_method VARCHAR(64) NOT NULL DEFAULT 'UPI',
  payment_date DATE NOT NULL,
  notes TEXT NULL,
  created_at DATETIME(6) NOT NULL,
  KEY idx_invoice_payments_invoice_id (invoice_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
