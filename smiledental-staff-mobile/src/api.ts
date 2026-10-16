import * as SecureStore from 'expo-secure-store';

import type {
  AdminOperationsOverview, AdminSession, AppSession, Appointment,
  AppointmentStats, Attendance, ClinicBranch, Coordinates, EvidenceAsset,
  LeaveRequest, LeaveType, MobileSession,
} from './types';

export const API_URL = (
  process.env.EXPO_PUBLIC_SMILEDENTAL_API_URL ||
  process.env.EXPO_PUBLIC_AIRZEN_API_URL ||
  'http://127.0.0.1:8000'
).replace(/\/$/, '');

const SESSION_KEY = 'smiledental.staff.session';
const DEVICE_KEY = 'smiledental.staff.device-id';
export const ACTIVE_BRANCH_KEY = 'smiledental.staff.active-branch';

let sessionCache: AppSession | null = null;

function randomId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

export function idempotencyKey(action: string) {
  return `${action}-${randomId()}`;
}

export async function deviceId() {
  let value = await SecureStore.getItemAsync(DEVICE_KEY);
  if (!value) {
    value = randomId();
    await SecureStore.setItemAsync(DEVICE_KEY, value);
  }
  return value;
}

export async function getSelectedBranch(): Promise<ClinicBranch> {
  const branch = await SecureStore.getItemAsync(ACTIVE_BRANCH_KEY);
  return branch || 'ECIL / Kapra (Main Clinic)';
}

export async function setSelectedBranch(branch: ClinicBranch): Promise<void> {
  await SecureStore.setItemAsync(ACTIVE_BRANCH_KEY, branch);
}

export async function loadSession(): Promise<AppSession | null> {
  if (sessionCache) return sessionCache;
  const stored = await SecureStore.getItemAsync(SESSION_KEY);
  if (!stored) return null;
  try {
    sessionCache = JSON.parse(stored) as AppSession;
    if (!(sessionCache as any).role) {
      (sessionCache as any).role = (sessionCache as any).admin ? 'admin' : 'staff';
    }
    return sessionCache;
  } catch {
    await SecureStore.deleteItemAsync(SESSION_KEY);
    return null;
  }
}

export async function saveSession(session: AppSession | null): Promise<void> {
  sessionCache = session;
  if (session) {
    await SecureStore.setItemAsync(SESSION_KEY, JSON.stringify(session));
  } else {
    await SecureStore.deleteItemAsync(SESSION_KEY);
  }
}

async function parseResponse(response: Response) {
  const text = await response.text();
  let body: any = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { error: text || `Request failed (${response.status})` };
  }
  if (!response.ok) {
    throw new Error(body.detail || body.error || `Request failed (${response.status})`);
  }
  return body;
}

async function refreshSession(current: MobileSession): Promise<MobileSession> {
  const response = await fetch(`${API_URL}/api/mobile/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: current.session_id,
      refresh_token: current.refresh_token,
    }),
  });
  const updated = (await parseResponse(response)) as MobileSession;
  await saveSession(updated);
  return updated;
}

export async function apiRequest(path: string, init: RequestInit = {}, retry = true): Promise<any> {
  const current = await loadSession();
  const headers = new Headers(init.headers || {});
  if (current?.access_token) {
    headers.set('Authorization', `Bearer ${current.access_token}`);
  }
  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (response.status === 401 && retry && current?.role === 'staff' && !path.startsWith('/api/mobile/auth/')) {
    try {
      const updated = await refreshSession(current);
      headers.set('Authorization', `Bearer ${updated.access_token}`);
      return parseResponse(await fetch(`${API_URL}${path}`, { ...init, headers }));
    } catch {
      await saveSession(null);
    }
  }
  return parseResponse(response);
}

export async function login(username: string, pin: string, appVersion = '2.0.0'): Promise<MobileSession> {
  const result = (await apiRequest('/api/mobile/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username,
      pin,
      device_id: await deviceId(),
      device_name: 'Smile Dental Clinic Handheld',
      app_version: appVersion,
    }),
  })) as MobileSession;
  result.role = 'staff';
  await saveSession(result);
  return result;
}

export async function adminLogin(username: string, pinOrPassword: string, appVersion = '2.0.0'): Promise<AdminSession> {
  const result = (await apiRequest('/api/mobile/auth/admin-login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username,
      password: pinOrPassword,
      device_id: await deviceId(),
      device_name: 'Smile Dental Admin Handheld',
      app_version: appVersion,
    }),
  })) as AdminSession;
  result.role = 'admin';
  await saveSession(result);
  return result;
}

export async function logout(): Promise<void> {
  const current = await loadSession();
  if (current?.role === 'staff') {
    try {
      await apiRequest('/api/mobile/auth/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: current.session_id }),
      });
    } catch (_) {}
  }
  await saveSession(null);
}

// Dental Appointment & Stats APIs
export async function getAppointmentStats(branch = '', doctor = ''): Promise<AppointmentStats> {
  const params = new URLSearchParams();
  if (branch) params.set('branch_name', branch);
  if (doctor) params.set('doctor_name', doctor);
  const q = params.toString() ? `?${params.toString()}` : '';
  return apiRequest(`/api/appointments/stats${q}`);
}

export async function listAppointments(date = '', branch = ''): Promise<Appointment[]> {
  const params = new URLSearchParams();
  if (date) params.set('date', date);
  if (branch) params.set('branch_name', branch);
  const q = params.toString() ? `?${params.toString()}` : '';
  const res = await apiRequest(`/api/appointments${q}`);
  return res.appointments || res || [];
}

export async function updateAppointmentStatus(
  appointmentId: string,
  status: string,
  notes = '',
  chairNumber = ''
): Promise<any> {
  return apiRequest(`/api/appointments/${encodeURIComponent(appointmentId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      status,
      notes,
      chair_number: chairNumber,
    }),
  });
}

// Attendance APIs
export async function todayAttendance(staffId?: string): Promise<Attendance | null> {
  const today = new Date().toISOString().slice(0, 10);
  const params = new URLSearchParams({ date_from: today, date_to: today });
  if (staffId) params.set('staff_id', staffId);
  try {
    const res = await apiRequest(`/api/staff-attendance?${params.toString()}`);
    const rows = res.attendance || [];
    return rows.length ? rows[0] : null;
  } catch {
    return null;
  }
}

export async function quickCheckIn(staffId: string, workDate: string, notes = ''): Promise<any> {
  return apiRequest('/api/staff-attendance/quick-record', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      staff_id: staffId,
      action: 'check_in',
      work_date: workDate,
      notes: notes || 'Check-in recorded via Smile Dental Staff App',
    }),
  });
}

export async function quickCheckOut(staffId: string, workDate: string): Promise<any> {
  return apiRequest('/api/staff-attendance/quick-record', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      staff_id: staffId,
      action: 'check_out',
      work_date: workDate,
    }),
  });
}

// Leave Request APIs
export async function listLeaveRequests(staffId = ''): Promise<LeaveRequest[]> {
  const params = new URLSearchParams();
  if (staffId) params.set('staff_id', staffId);
  const q = params.toString() ? `?${params.toString()}` : '';
  const res = await apiRequest(`/api/staff-leave-requests${q}`);
  return res.leave_requests || [];
}

export async function createLeaveRequest(
  staffId: string,
  leaveType: LeaveType,
  startDate: string,
  endDate: string,
  reason: string
): Promise<any> {
  return apiRequest('/api/staff-leave-requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      staff_id: staffId,
      leave_type: leaveType,
      start_date: startDate,
      end_date: endDate,
      reason,
    }),
  });
}

export async function adminDecideLeave(
  requestId: string,
  decision: 'approved' | 'declined',
  adminNote = ''
): Promise<any> {
  return apiRequest(`/api/staff-leave-requests/${encodeURIComponent(requestId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      decision,
      admin_note: adminNote,
    }),
  });
}

// Clinic Operations Overview & Multi-Branch Revenue
export async function adminOperationsOverview(workDate = ''): Promise<AdminOperationsOverview> {
  const q = workDate ? `?work_date=${encodeURIComponent(workDate)}` : '';
  return apiRequest(`/api/staff-operations/overview${q}`);
}

export async function getRevenueSummary(branchName = ''): Promise<any> {
  const q = branchName ? `?branch_name=${encodeURIComponent(branchName)}` : '';
  return apiRequest(`/api/billing/revenue-summary${q}`);
}
