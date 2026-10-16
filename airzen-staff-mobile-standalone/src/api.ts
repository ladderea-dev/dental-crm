import * as SecureStore from 'expo-secure-store';

import type { Appointment, Coordinates, EvidenceAsset, MobileSession } from './types';

const API_URL = (process.env.EXPO_PUBLIC_AIRZEN_API_URL || 'https://voice.airzensg.com').replace(/\/$/, '');
const SESSION_KEY = 'airzen.staff.session';
const DEVICE_KEY = 'airzen.staff.device-id';
export const ACTIVE_APPOINTMENT_KEY = 'airzen.staff.active-appointment';

let sessionCache: MobileSession | null = null;

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
    sessionCache = JSON.parse(stored) as MobileSession;
    return sessionCache;
  } catch {
    await SecureStore.deleteItemAsync(SESSION_KEY);
    return null;
  }
}

async function saveSession(session: MobileSession | null) {
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
  if (response.status === 401 && retry && current && !path.startsWith('/api/mobile/auth/')) {
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

export async function logout() {
  try {
    await apiRequest('/api/mobile/auth/logout', { method: 'POST' }, false);
  } finally {
    await saveSession(null);
    await SecureStore.deleteItemAsync(ACTIVE_APPOINTMENT_KEY);
  }
}

export async function listAppointments() {
  const result = await apiRequest('/api/mobile/appointments');
  return (result.appointments || []) as Appointment[];
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

export async function verifyOtp(appointmentId: string, otp: string) {
  return apiRequest(`/api/mobile/appointments/${appointmentId}/verify-otp`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ otp, idempotency_key: idempotencyKey('otp') }),
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
