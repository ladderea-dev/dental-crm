import * as SecureStore from 'expo-secure-store';

import type {
  AdminOperationsOverview, AdminSession, AppSession, Appointment, Attendance,
  Coordinates, EvidenceAsset, LeaveRequest, MobileSession,
} from './types';

const API_URL = (process.env.EXPO_PUBLIC_AIRZEN_API_URL || 'https://voice.airzensg.com').replace(/\/$/, '');
const SESSION_KEY = 'airzen.staff.session';
const DEVICE_KEY = 'airzen.staff.device-id';
export const ACTIVE_APPOINTMENT_KEY = 'airzen.staff.active-appointment';

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

export async function loadSession() {
  if (sessionCache) return sessionCache;
  const stored = await SecureStore.getItemAsync(SESSION_KEY);
  if (!stored) return null;
  try {
    sessionCache = JSON.parse(stored) as AppSession;
    if (!(sessionCache as any).role) (sessionCache as any).role = (sessionCache as any).admin ? 'admin' : 'staff';
    return sessionCache;
  } catch {
    await SecureStore.deleteItemAsync(SESSION_KEY);
    return null;
  }
}

async function saveSession(session: AppSession | null) {
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
  if (!response.ok) throw new Error(body.detail || body.error || `Request failed (${response.status})`);
  return body;
}

async function refreshSession(current: MobileSession) {
  const response = await fetch(`${API_URL}/api/mobile/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: current.session_id, refresh_token: current.refresh_token }),
  });
  const updated = (await parseResponse(response)) as MobileSession;
  await saveSession(updated);
  return updated;
}

export async function apiRequest(path: string, init: RequestInit = {}, retry = true): Promise<any> {
  const current = await loadSession();
  const headers = new Headers(init.headers || {});
  if (current?.access_token) headers.set('Authorization', `Bearer ${current.access_token}`);
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

export async function login(username: string, pin: string, appVersion = '1.0.0') {
  const result = (await apiRequest('/api/mobile/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username,
      pin,
      device_id: await deviceId(),
      device_name: 'Android device',
      app_version: appVersion,
    }),
  }, false)) as MobileSession;
  await saveSession(result);
  return result;
}

export async function adminLogin(username: string, password: string, appVersion = '1.0.0') {
  const result = (await apiRequest('/api/mobile/admin/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username,
      password,
      device_id: await deviceId(),
      device_name: 'Android device',
      app_version: appVersion,
    }),
  }, false)) as AdminSession;
  await saveSession(result);
  return result;
}

export async function logout() {
  const current = await loadSession();
  try {
    const path = current?.role === 'admin' ? '/api/mobile/admin/auth/logout' : '/api/mobile/auth/logout';
    await apiRequest(path, { method: 'POST' }, false);
  } finally {
    await saveSession(null);
    await SecureStore.deleteItemAsync(ACTIVE_APPOINTMENT_KEY);
  }
}

export async function adminOperationsOverview(workDate: string) {
  const result = await apiRequest(`/api/mobile/admin/overview?work_date=${encodeURIComponent(workDate)}`);
  return result as AdminOperationsOverview;
}

export async function adminDecideLeave(requestId: string, decision: 'approved' | 'declined', adminNote: string) {
  const result = await apiRequest(`/api/mobile/admin/leave-requests/${encodeURIComponent(requestId)}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, admin_note: adminNote }),
  });
  return result.leave_request as LeaveRequest;
}

export async function listAppointments() {
  const result = await apiRequest('/api/mobile/appointments');
  return (result.appointments || []) as Appointment[];
}

export async function todayAttendance() {
  const result = await apiRequest('/api/mobile/attendance/today');
  return result as { work_date: string; attendance: Attendance | null };
}

async function attendanceAction(action: 'check-in' | 'check-out', coords: Coordinates, notes = '') {
  const result = await apiRequest(`/api/mobile/attendance/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...coords, notes }),
  });
  return result as { work_date: string; attendance: Attendance };
}

export const checkIn = (coords: Coordinates, notes = '') => attendanceAction('check-in', coords, notes);
export const checkOut = (coords: Coordinates, notes = '') => attendanceAction('check-out', coords, notes);

export async function listLeaveRequests() {
  const result = await apiRequest('/api/mobile/leave-requests');
  return (result.leave_requests || []) as LeaveRequest[];
}

export async function createLeaveRequest(input: {
  leave_type: LeaveRequest['leave_type']; start_date: string; end_date: string; reason: string;
}) {
  const result = await apiRequest('/api/mobile/leave-requests', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
  return result.leave_request as LeaveRequest;
}

export async function appointmentDetail(appointmentId: string) {
  return apiRequest(`/api/mobile/appointments/${appointmentId}`);
}

export async function enRoute(appointmentId: string, coords: Coordinates) {
  return apiRequest(`/api/mobile/appointments/${appointmentId}/en-route`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...coords, idempotency_key: idempotencyKey('en-route') }),
  });
}

export async function sendLocation(appointmentId: string, location: any) {
  const coords = location.coords;
  return apiRequest(`/api/mobile/appointments/${appointmentId}/location`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      latitude: coords.latitude,
      longitude: coords.longitude,
      accuracy_meters: coords.accuracy,
      speed_mps: coords.speed,
      heading_degrees: coords.heading,
      is_mocked: Boolean(location.mocked),
      captured_at: new Date(location.timestamp).toISOString(),
      idempotency_key: `${location.timestamp}-${coords.latitude.toFixed(6)}-${coords.longitude.toFixed(6)}`,
    }),
  });
}

function appendAsset(form: FormData, field: string, asset: EvidenceAsset) {
  form.append(field, {
    uri: asset.uri,
    name: asset.fileName || `${field}.jpg`,
    type: asset.mimeType || 'image/jpeg',
  } as any);
}

export async function arrive(appointmentId: string, selfie: EvidenceAsset, coords: Coordinates) {
  const form = new FormData();
  appendAsset(form, 'selfie', selfie);
  form.append('latitude', String(coords.latitude));
  form.append('longitude', String(coords.longitude));
  if (coords.accuracy_meters != null) form.append('accuracy_meters', String(coords.accuracy_meters));
  form.append('idempotency_key', idempotencyKey('arrive'));
  return apiRequest(`/api/mobile/appointments/${appointmentId}/arrive`, { method: 'POST', body: form });
}

export async function verifyStartCode(appointmentId: string, code: string) {
  return apiRequest(`/api/mobile/appointments/${appointmentId}/verify-start-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, idempotency_key: idempotencyKey('start-code') }),
  });
}

export async function completeJob(
  appointmentId: string,
  proof: EvidenceAsset,
  payment: { amount: string; method: string; reference: string; notes: string },
  coords: Coordinates,
) {
  const form = new FormData();
  appendAsset(form, 'payment_proof', proof);
  form.append('payment_amount', payment.amount);
  form.append('payment_method', payment.method);
  form.append('payment_reference', payment.reference);
  form.append('notes', payment.notes);
  form.append('latitude', String(coords.latitude));
  form.append('longitude', String(coords.longitude));
  if (coords.accuracy_meters != null) form.append('accuracy_meters', String(coords.accuracy_meters));
  form.append('idempotency_key', idempotencyKey('complete'));
  return apiRequest(`/api/mobile/appointments/${appointmentId}/complete`, { method: 'POST', body: form });
}
