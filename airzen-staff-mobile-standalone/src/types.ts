export type Staff = {
  id: string;
  name: string;
  email: string;
  whatsapp_number: string;
  role: string;
  timezone: string;
  username: string;
};

export type MobileSession = {
  access_token: string;
  access_token_expires_in: number;
  refresh_token: string;
  refresh_token_expires_in: number;
  session_id: string;
  staff: Staff;
};

export type Appointment = {
  id: string;
  name: string;
  phone: string;
  date: string;
  time: string;
  service: string;
  status: string;
  service_address?: string;
  location_latitude?: number | null;
  location_longitude?: number | null;
  location_url?: string;
  mobile_job_state?: string | null;
  last_location_at?: string | null;
  intake_data?: Record<string, unknown> | string | null;
};

export type EvidenceAsset = {
  uri: string;
  mimeType?: string | null;
  fileName?: string | null;
};

export type Coordinates = {
  latitude: number;
  longitude: number;
  accuracy_meters?: number | null;
};
