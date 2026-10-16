export type ClinicBranch =
  | 'ECIL / Kapra (Main Clinic)'
  | 'AS Rao Nagar Branch'
  | 'Sainikpuri Branch'
  | string;

export type StaffRole =
  | 'doctor'
  | 'dentist'
  | 'implantologist'
  | 'cosmetic_dentist'
  | 'orthodontist'
  | 'senior_nurse'
  | 'junior_nurse'
  | 'receptionist'
  | 'clinic_manager'
  | string;

export type Staff = {
  id: string;
  name: string;
  email: string;
  whatsapp_number: string;
  role: StaffRole;
  timezone: string;
  username: string;
  branch_name?: ClinicBranch;
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

export type AppointmentStatus =
  | 'booked'
  | 'waiting'
  | 'in_operatory'
  | 'in_progress'
  | 'completed'
  | 'cancelled';

export type Appointment = {
  id: string;
  name: string;
  phone: string;
  date: string;
  time: string;
  service: string;
  status: AppointmentStatus | string;
  branch_name?: ClinicBranch;
  doctor_name?: string;
  chair_number?: string | null;
  service_address?: string;
  clinical_notes?: string;
  prescription?: string;
  intake_data?: Record<string, unknown> | string | null;
  mobile_job_state?: string | null;
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
  staff_id?: string;
  staff_name?: string;
  staff_role?: string;
  work_date: string;
  check_in_at: string;
  check_out_at?: string | null;
  branch_name?: ClinicBranch;
  notes?: string;
  check_in_accuracy_meters?: number | null;
  check_out_accuracy_meters?: number | null;
};

export type LeaveType = 'medical' | 'annual' | 'emergency' | 'casual' | 'unpaid' | 'other';

export type LeaveRequest = {
  id: string;
  staff_id: string;
  staff_name?: string;
  leave_type: LeaveType;
  start_date: string;
  end_date: string;
  reason: string;
  status: 'pending' | 'approved' | 'declined' | 'cancelled';
  admin_note?: string;
  created_at: string;
};

export type AppointmentStats = {
  today: number;
  this_week: number;
  this_month: number;
  total: number;
  completed: number;
  in_progress: number;
  booked: number;
  by_doctor?: Record<string, number>;
  by_branch?: Record<string, number>;
  date_today?: string;
  week_range?: string;
  month_range?: string;
};

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
  doctor_stats?: AppointmentStats;
  branch_revenue?: Array<{ branch_name: string; billed: number; collected: number }>;
};
