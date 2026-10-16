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
  role: 'staff';
  access_token: string;
  access_token_expires_in: number;
  refresh_token: string;
  refresh_token_expires_in: number;
  session_id: string;
  staff: Staff;
};

export type AdminSession = {
  role: 'admin';
  access_token: string;
  access_token_expires_in: number;
  admin: { username: string; name: string };
};

export type AppSession = MobileSession | AdminSession;

export type AdminOperationsOverview = {
  work_date: string;
  generated_at: string;
  summary: {
    staff_total: number;
    checked_in: number;
    checked_out: number;
    absent: number;
    on_leave: number;
    active_jobs: number;
    tracking_jobs: number;
    pending_leave: number;
    pending_jobs: number;
    completed_jobs: number;
    cancelled_jobs: number;
    appointments_total: number;
  };
  staff: Array<Record<string, any>>;
  appointments: Array<Appointment & { staff_id?: string; staff_name?: string }>;
  active_jobs: Array<Record<string, any>>;
  pending_leave: Array<LeaveRequest & { staff_id?: string; staff_name?: string }>;
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

export type Attendance = {
  id: string;
  work_date: string;
  check_in_at: string;
  check_out_at?: string | null;
  check_in_accuracy_meters?: number | null;
  check_out_accuracy_meters?: number | null;
};

export type LeaveRequest = {
  id: string;
  leave_type: 'annual' | 'medical' | 'emergency' | 'unpaid' | 'other';
  start_date: string;
  end_date: string;
  reason: string;
  status: 'pending' | 'approved' | 'declined' | 'cancelled';
  admin_note?: string;
  created_at: string;
};
