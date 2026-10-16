-- V2 Unified Enhancements Migration
-- Multi-User RBAC, Telecaller Workspace, Dynamic Pipeline, Multi-Tenancy Foundation

-- 1. Multi-Tenancy Foundation
CREATE TABLE IF NOT EXISTS tenants (
  id CHAR(36) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  slug VARCHAR(64) UNIQUE NOT NULL,
  domain VARCHAR(255) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO tenants (id, name, slug, domain, created_at, updated_at)
VALUES ('default-tenant', 'AirZen Main', 'default', 'voice.airzensg.com', NOW(6), NOW(6))
ON DUPLICATE KEY UPDATE updated_at = NOW(6);

-- 2. Users and Roles (RBAC)
CREATE TABLE IF NOT EXISTS users (
  id CHAR(36) PRIMARY KEY,
  tenant_id CHAR(36) NOT NULL DEFAULT 'default-tenant',
  username VARCHAR(64) NOT NULL,
  email VARCHAR(255) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  full_name VARCHAR(255) NOT NULL,
  role VARCHAR(64) NOT NULL DEFAULT 'telecaller', -- admin, manager, telecaller, support
  active BOOLEAN NOT NULL DEFAULT TRUE,
  last_login_at DATETIME(6) NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  UNIQUE KEY uq_users_tenant_username (tenant_id, username),
  UNIQUE KEY uq_users_tenant_email (tenant_id, email),
  KEY idx_users_role (role),
  KEY idx_users_active (active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. Lead Statuses Enhancements for Dynamic Pipelines
ALTER TABLE lead_statuses 
  ADD COLUMN IF NOT EXISTS sort_order INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS stage_category VARCHAR(64) NOT NULL DEFAULT 'open', -- open, in_progress, won, lost
  ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT FALSE;

-- 4. Call Logs Enhancements for Human Telecallers & Inbound Calls
ALTER TABLE call_logs
  ADD COLUMN IF NOT EXISTS caller_type VARCHAR(32) NOT NULL DEFAULT 'ai', -- ai, telecaller
  ADD COLUMN IF NOT EXISTS telecaller_id CHAR(36) NULL,
  ADD COLUMN IF NOT EXISTS telecaller_username VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS call_direction VARCHAR(16) NOT NULL DEFAULT 'outbound', -- outbound, inbound
  ADD COLUMN IF NOT EXISTS disposition VARCHAR(64) NULL;

-- 5. Contacts Enhancements
ALTER TABLE crm_contacts
  ADD COLUMN IF NOT EXISTS tenant_id CHAR(36) NOT NULL DEFAULT 'default-tenant',
  ADD COLUMN IF NOT EXISTS assigned_telecaller_id CHAR(36) NULL,
  ADD COLUMN IF NOT EXISTS assigned_telecaller VARCHAR(64) NULL;
