-- AirZen staff mobile application schema (MySQL 8.4+)
-- Additive and idempotent: no existing AirZen table is altered or deleted.

CREATE TABLE IF NOT EXISTS staff_mobile_credentials (
  staff_id CHAR(36) PRIMARY KEY,
  username VARCHAR(64) NOT NULL,
  pin_hash VARCHAR(255) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  failed_attempts INT NOT NULL DEFAULT 0,
  locked_until DATETIME(6) NULL,
  pin_changed_at DATETIME(6) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_staff_mobile_credentials_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE CASCADE,
  UNIQUE KEY uq_staff_mobile_username (username),
  KEY idx_staff_mobile_credentials_active (active, locked_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS staff_mobile_sessions (
  id CHAR(36) PRIMARY KEY,
  staff_id CHAR(36) NOT NULL,
  refresh_token_hash CHAR(64) NOT NULL,
  device_id VARCHAR(191) NOT NULL,
  device_name VARCHAR(255) NOT NULL DEFAULT '',
  platform VARCHAR(32) NOT NULL DEFAULT 'android',
  app_version VARCHAR(64) NOT NULL DEFAULT '',
  last_seen_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  revoked_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_staff_mobile_sessions_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE CASCADE,
  UNIQUE KEY uq_staff_mobile_session_refresh (refresh_token_hash),
  KEY idx_staff_mobile_sessions_staff_active (staff_id, revoked_at, expires_at),
  KEY idx_staff_mobile_sessions_device (staff_id, device_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointment_job_events (
  id CHAR(36) PRIMARY KEY,
  appointment_id CHAR(36) NOT NULL,
  staff_id CHAR(36) NOT NULL,
  session_id CHAR(36) NULL,
  event_type VARCHAR(64) NOT NULL,
  event_data JSON NULL,
  latitude DOUBLE NULL,
  longitude DOUBLE NULL,
  accuracy_meters DOUBLE NULL,
  idempotency_key CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_appointment_job_events_appointment
    FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
  CONSTRAINT fk_appointment_job_events_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE RESTRICT,
  CONSTRAINT fk_appointment_job_events_session
    FOREIGN KEY (session_id) REFERENCES staff_mobile_sessions(id) ON DELETE SET NULL,
  UNIQUE KEY uq_appointment_job_events_idempotency (idempotency_key),
  KEY idx_appointment_job_events_timeline (appointment_id, created_at),
  KEY idx_appointment_job_events_staff (staff_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointment_tracking_events (
  id CHAR(36) PRIMARY KEY,
  appointment_id CHAR(36) NOT NULL,
  staff_id CHAR(36) NOT NULL,
  session_id CHAR(36) NOT NULL,
  latitude DOUBLE NOT NULL,
  longitude DOUBLE NOT NULL,
  accuracy_meters DOUBLE NULL,
  speed_mps DOUBLE NULL,
  heading_degrees DOUBLE NULL,
  battery_percent INT NULL,
  is_mocked BOOLEAN NOT NULL DEFAULT FALSE,
  captured_at DATETIME(6) NOT NULL,
  idempotency_key CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_appointment_tracking_appointment
    FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
  CONSTRAINT fk_appointment_tracking_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE RESTRICT,
  CONSTRAINT fk_appointment_tracking_session
    FOREIGN KEY (session_id) REFERENCES staff_mobile_sessions(id) ON DELETE CASCADE,
  UNIQUE KEY uq_appointment_tracking_idempotency (idempotency_key),
  KEY idx_appointment_tracking_latest (appointment_id, captured_at),
  KEY idx_appointment_tracking_staff (staff_id, captured_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointment_evidence (
  id CHAR(36) PRIMARY KEY,
  appointment_id CHAR(36) NOT NULL,
  staff_id CHAR(36) NOT NULL,
  session_id CHAR(36) NOT NULL,
  evidence_type VARCHAR(64) NOT NULL,
  storage_key VARCHAR(512) NOT NULL,
  sha256 CHAR(64) NOT NULL,
  content_type VARCHAR(100) NOT NULL,
  file_size BIGINT UNSIGNED NOT NULL,
  latitude DOUBLE NULL,
  longitude DOUBLE NULL,
  accuracy_meters DOUBLE NULL,
  captured_at DATETIME(6) NOT NULL,
  metadata_json JSON NULL,
  idempotency_key CHAR(64) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_appointment_evidence_appointment
    FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
  CONSTRAINT fk_appointment_evidence_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE RESTRICT,
  CONSTRAINT fk_appointment_evidence_session
    FOREIGN KEY (session_id) REFERENCES staff_mobile_sessions(id) ON DELETE RESTRICT,
  UNIQUE KEY uq_appointment_evidence_storage (storage_key),
  UNIQUE KEY uq_appointment_evidence_idempotency (idempotency_key),
  KEY idx_appointment_evidence_timeline (appointment_id, evidence_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS appointment_start_otps (
  appointment_id CHAR(36) PRIMARY KEY,
  staff_id CHAR(36) NOT NULL,
  otp_hash CHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  attempt_count INT NOT NULL DEFAULT 0,
  max_attempts INT NOT NULL DEFAULT 5,
  issued_at DATETIME(6) NOT NULL,
  expires_at DATETIME(6) NOT NULL,
  verified_at DATETIME(6) NULL,
  verified_by_session_id CHAR(36) NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_appointment_start_otps_appointment
    FOREIGN KEY (appointment_id) REFERENCES appointments(id) ON DELETE CASCADE,
  CONSTRAINT fk_appointment_start_otps_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE RESTRICT,
  CONSTRAINT fk_appointment_start_otps_session
    FOREIGN KEY (verified_by_session_id) REFERENCES staff_mobile_sessions(id) ON DELETE SET NULL,
  KEY idx_appointment_start_otps_due (status, expires_at),
  KEY idx_appointment_start_otps_staff (staff_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS staff_attendance (
  id CHAR(36) PRIMARY KEY,
  staff_id CHAR(36) NOT NULL,
  work_date DATE NOT NULL,
  check_in_at DATETIME(6) NOT NULL,
  check_in_latitude DOUBLE NULL,
  check_in_longitude DOUBLE NULL,
  check_in_accuracy_meters DOUBLE NULL,
  check_in_session_id CHAR(36) NULL,
  check_out_at DATETIME(6) NULL,
  check_out_latitude DOUBLE NULL,
  check_out_longitude DOUBLE NULL,
  check_out_accuracy_meters DOUBLE NULL,
  check_out_session_id CHAR(36) NULL,
  notes VARCHAR(1000) NOT NULL DEFAULT '',
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_staff_attendance_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE CASCADE,
  CONSTRAINT fk_staff_attendance_check_in_session
    FOREIGN KEY (check_in_session_id) REFERENCES staff_mobile_sessions(id) ON DELETE SET NULL,
  CONSTRAINT fk_staff_attendance_check_out_session
    FOREIGN KEY (check_out_session_id) REFERENCES staff_mobile_sessions(id) ON DELETE SET NULL,
  CONSTRAINT chk_staff_attendance_checkout_order
    CHECK (check_out_at IS NULL OR check_out_at >= check_in_at),
  UNIQUE KEY uq_staff_attendance_day (staff_id, work_date),
  KEY idx_staff_attendance_daily (work_date, check_in_at),
  KEY idx_staff_attendance_open (staff_id, check_out_at, work_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS staff_leave_requests (
  id CHAR(36) PRIMARY KEY,
  staff_id CHAR(36) NOT NULL,
  leave_type VARCHAR(32) NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  reason VARCHAR(2000) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'pending',
  admin_note VARCHAR(2000) NOT NULL DEFAULT '',
  reviewed_at DATETIME(6) NULL,
  reviewed_by VARCHAR(191) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  CONSTRAINT fk_staff_leave_requests_staff
    FOREIGN KEY (staff_id) REFERENCES appointment_staff(id) ON DELETE CASCADE,
  CONSTRAINT chk_staff_leave_date_order CHECK (end_date >= start_date),
  CONSTRAINT chk_staff_leave_type CHECK (
    leave_type IN ('annual', 'medical', 'emergency', 'unpaid', 'other')
  ),
  CONSTRAINT chk_staff_leave_status CHECK (
    status IN ('pending', 'approved', 'declined', 'cancelled')
  ),
  KEY idx_staff_leave_staff_dates (staff_id, start_date, end_date),
  KEY idx_staff_leave_review_queue (status, start_date, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
