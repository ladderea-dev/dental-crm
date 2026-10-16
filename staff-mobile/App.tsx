import { StatusBar } from 'expo-status-bar';
import * as ImagePicker from 'expo-image-picker';
import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator, Alert, Linking, Pressable, RefreshControl, SafeAreaView,
  ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';
import { initialWindowMetrics, SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import {
  adminDecideLeave, adminLogin, adminOperationsOverview, appointmentDetail, arrive,
  checkIn, checkOut, completeJob, createLeaveRequest, enRoute, listAppointments,
  listLeaveRequests, loadSession, login, logout, todayAttendance, verifyStartCode,
} from './src/api';
import { currentCoordinates, startJobTracking, stopJobTracking } from './src/locationTask';
import type {
  AdminOperationsOverview, AdminSession, AppSession, Appointment, Attendance,
  EvidenceAsset, LeaveRequest, MobileSession,
} from './src/types';

const colors = {
  ink: '#102A22', muted: '#64746E', green: '#0B6E4F', greenSoft: '#DDF3E9',
  surface: '#FFFFFF', background: '#F4F8F6', border: '#DCE7E1', danger: '#A63D40',
};

function Button({ title, onPress, disabled = false, kind = 'primary' }: {
  title: string; onPress: () => void; disabled?: boolean; kind?: 'primary' | 'secondary' | 'danger';
}) {
  return <Pressable accessibilityRole="button" disabled={disabled} onPress={onPress} style={({ pressed }) => [
    styles.button, kind === 'secondary' && styles.buttonSecondary,
    kind === 'danger' && styles.buttonDanger, (pressed || disabled) && styles.buttonMuted,
  ]}><Text style={[styles.buttonText, kind === 'secondary' && styles.buttonSecondaryText]}>{title}</Text></Pressable>;
}

function Field({ label, value, onChangeText, secureTextEntry = false, keyboardType = 'default', placeholder = '' }: {
  label: string; value: string; onChangeText: (value: string) => void; secureTextEntry?: boolean;
  keyboardType?: 'default' | 'numeric' | 'phone-pad' | 'email-address' | 'decimal-pad'; placeholder?: string;
}) {
  return <View style={styles.field}><Text style={styles.label}>{label}</Text><TextInput
    value={value} onChangeText={onChangeText} secureTextEntry={secureTextEntry}
    keyboardType={keyboardType} placeholder={placeholder} placeholderTextColor="#91A099" style={styles.input}
  /></View>;
}

function stateFor(appointment: Appointment) {
  if (appointment.status === 'completed') return 'completed';
  if (appointment.status === 'in_progress') return 'in_progress';
  return appointment.mobile_job_state || 'booked';
}

function stateLabel(state: string) {
  return ({ booked: 'Assigned', en_route: 'Travelling', arrived: 'Awaiting start code',
    job_started: 'In progress', in_progress: 'In progress', completed: 'Completed' } as Record<string, string>)[state]
    || state.replace(/_/g, ' ');
}

type StaffSection = 'home' | 'jobs' | 'attendance' | 'leave';
type AdminSection = 'overview' | 'team' | 'jobs' | 'leave';

function formatWorkDate(value: string) {
  if (!value) return 'Today';
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString('en-SG', {
    weekday: 'long', day: 'numeric', month: 'short',
  });
}

function BottomNav({ current, onSelect }: { current: StaffSection; onSelect: (section: StaffSection) => void }) {
  const insets = useSafeAreaInsets();
  const items: Array<{ id: StaffSection; icon: string; label: string }> = [
    { id: 'home', icon: '⌂', label: 'Home' },
    { id: 'jobs', icon: '▣', label: 'Jobs' },
    { id: 'attendance', icon: '◷', label: 'Attendance' },
    { id: 'leave', icon: '✦', label: 'Leave' },
  ];
  return <View style={[styles.bottomNav, { paddingBottom: Math.max(insets.bottom, 8) }]}>{items.map(item => {
    const active = current === item.id;
    return <Pressable
      key={item.id} accessibilityRole="button" accessibilityLabel={item.label}
      testID={`bottom-nav-${item.id}`} hitSlop={6}
      onPress={() => onSelect(item.id)} style={({ pressed }) => [styles.bottomNavItem, pressed && styles.pressed]}
    >
      <Text style={[styles.bottomNavIcon, active && styles.bottomNavIconActive]}>{item.icon}</Text>
      <Text style={[styles.bottomNavLabel, active && styles.bottomNavLabelActive]}>{item.label}</Text>
      {active && <View style={styles.bottomNavMarker} />}
    </Pressable>;
  })}</View>;
}

function QuickTile({ icon, title, detail, onPress }: { icon: string; title: string; detail: string; onPress: () => void }) {
  return <Pressable onPress={onPress} style={({ pressed }) => [styles.quickTile, pressed && styles.pressed]}>
    <View style={styles.quickIcon}><Text style={styles.quickIconText}>{icon}</Text></View>
    <Text style={styles.quickTitle}>{title}</Text><Text style={styles.quickDetail}>{detail}</Text>
  </Pressable>;
}

function LoginScreen({ onLogin }: { onLogin: (session: AppSession) => void }) {
  const [role, setRole] = useState<'staff' | 'admin'>('staff');
  const [username, setUsername] = useState('');
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!username.trim() || (role === 'staff' && (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/.test(username.trim()) || !/^\d{6}$/.test(secret)))) {
      Alert.alert('Check login', role === 'staff' ? 'Enter your staff username and six-digit PIN.' : 'Enter your CRM administrator username and password.'); return;
    }
    if (role === 'admin' && !secret) {
      Alert.alert('Check login', 'Enter your CRM administrator username and password.'); return;
    }
    setBusy(true);
    try {
      onLogin(role === 'admin'
        ? await adminLogin(username.trim(), secret)
        : await login(username.trim().toLowerCase(), secret));
    }
    catch (error: any) { Alert.alert('Login failed', error.message || 'Please check your details.'); }
    finally { setBusy(false); }
  };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.loginWrap}>
    <View style={styles.brandMark}><Text style={styles.brandMarkText}>A</Text></View>
    <Text style={styles.title}>AirZen Operations</Text><Text style={styles.subtitle}>Secure staff and administrator workspace</Text>
    <View style={styles.loginRoleRow}>
      <Pressable testID="login-role-staff" onPress={() => { setRole('staff'); setSecret(''); }} style={[styles.loginRole, role === 'staff' && styles.loginRoleActive]}><Text style={role === 'staff' ? styles.loginRoleTextActive : styles.loginRoleText}>Staff</Text></Pressable>
      <Pressable testID="login-role-admin" onPress={() => { setRole('admin'); setSecret(''); }} style={[styles.loginRole, role === 'admin' && styles.loginRoleActive]}><Text style={role === 'admin' ? styles.loginRoleTextActive : styles.loginRoleText}>Admin</Text></Pressable>
    </View>
    <View style={styles.card}>
      <Field label={role === 'staff' ? 'Staff username' : 'CRM administrator username'} value={username} onChangeText={value => setUsername(role === 'staff' ? value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64) : value.slice(0, 191))} placeholder={role === 'staff' ? 'e.g. arun.tech' : 'Administrator username'} />
      <Field label={role === 'staff' ? 'Six-digit PIN' : 'Administrator password'} value={secret} onChangeText={value => setSecret(role === 'staff' ? value.replace(/\D/g, '').slice(0, 6) : value.slice(0, 512))} secureTextEntry keyboardType={role === 'staff' ? 'numeric' : 'default'} />
      <Button title={busy ? 'Signing in…' : 'Sign in'} onPress={submit} disabled={busy} />
    </View>
    <Text style={styles.privacy}>{role === 'staff' ? 'Only use your own staff account.' : 'Administrator access is short-lived and never stores your password.'} Customer data and management actions are audited.</Text>
  </View></SafeAreaView>;
}

function singaporeToday() {
  const parts = new Intl.DateTimeFormat('en-SG', {
    timeZone: 'Asia/Singapore', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date());
  const value = Object.fromEntries(parts.map(part => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function AdminBottomNav({ current, onSelect }: { current: AdminSection; onSelect: (section: AdminSection) => void }) {
  const insets = useSafeAreaInsets();
  const items: Array<{ id: AdminSection; icon: string; label: string }> = [
    { id: 'overview', icon: '⌂', label: 'Overview' },
    { id: 'team', icon: '◎', label: 'Team' },
    { id: 'jobs', icon: '▣', label: 'Jobs' },
    { id: 'leave', icon: '✦', label: 'Leave' },
  ];
  return <View style={[styles.bottomNav, { paddingBottom: Math.max(insets.bottom, 8) }]}>{items.map(item => {
    const active = current === item.id;
    return <Pressable key={item.id} accessibilityRole="button" accessibilityLabel={item.label}
      testID={`admin-nav-${item.id}`} hitSlop={6} onPress={() => onSelect(item.id)}
      style={({ pressed }) => [styles.bottomNavItem, pressed && styles.pressed]}>
      <Text style={[styles.bottomNavIcon, active && styles.bottomNavIconActive]}>{item.icon}</Text>
      <Text style={[styles.bottomNavLabel, active && styles.bottomNavLabelActive]}>{item.label}</Text>
      {active && <View style={styles.bottomNavMarker} />}
    </Pressable>;
  })}</View>;
}

function AdminMetric({ value, label }: { value: number; label: string }) {
  return <View style={styles.adminMetric}><Text style={styles.metricValue}>{value}</Text><Text style={styles.metricLabel}>{label}</Text></View>;
}

function AdminLeaveCard({ row, onUpdated }: { row: any; onUpdated: () => Promise<void> }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const decide = (decision: 'approved' | 'declined') => Alert.alert(
    decision === 'approved' ? 'Approve leave?' : 'Decline leave?',
    `${row.staff_name || 'This staff member'} · ${row.start_date} to ${row.end_date}`,
    [{ text: 'Cancel', style: 'cancel' }, { text: decision === 'approved' ? 'Approve' : 'Decline', style: decision === 'declined' ? 'destructive' : 'default', onPress: async () => {
      setBusy(true);
      try { await adminDecideLeave(row.id, decision, note.trim()); await onUpdated(); }
      catch (error: any) { Alert.alert('Leave decision failed', error.message || 'Please try again.'); }
      finally { setBusy(false); }
    } }],
  );
  return <View style={styles.card}><View style={styles.rowBetween}><Text style={styles.cardTitle}>{row.staff_name || 'Staff member'}</Text><View style={styles.pill}><Text style={styles.pillText}>{row.leave_type}</Text></View></View>
    <Text style={styles.detailStrong}>{row.start_date} to {row.end_date}</Text><Text style={styles.help}>{row.reason}</Text>
    <Field label="Decision note (optional)" value={note} onChangeText={setNote} placeholder="Coverage or decision note" />
    <View style={styles.buttonRow}><Button title="Approve" onPress={() => decide('approved')} disabled={busy} /><Button title="Decline" kind="danger" onPress={() => decide('declined')} disabled={busy} /></View>
  </View>;
}

function AdminWorkspace({ session, onSignedOut }: { session: AdminSession; onSignedOut: () => void }) {
  const [section, setSection] = useState<AdminSection>('overview');
  const [workDate, setWorkDate] = useState(singaporeToday());
  const [overview, setOverview] = useState<AdminOperationsOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const refresh = async () => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(workDate)) { Alert.alert('Check date', 'Use YYYY-MM-DD.'); return; }
    setLoading(true);
    try { setOverview(await adminOperationsOverview(workDate)); }
    catch (error: any) { Alert.alert('Admin dashboard unavailable', error.message || 'Please check your connection.'); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, []);
  const signOut = async () => { await logout(); onSignedOut(); };
  const openLocation = (location: any) => {
    if (location?.latitude == null || location?.longitude == null) return;
    Linking.openURL(`https://www.google.com/maps/search/?api=1&query=${location.latitude},${location.longitude}`);
  };
  const summary = overview?.summary;
  let body: React.ReactNode;
  if (section === 'team') body = <>
    <Text style={styles.sectionTitle}>Team activity</Text>
    {(overview?.staff || []).map(member => <View style={styles.card} key={member.id}>
      <View style={styles.rowBetween}><Text style={styles.cardTitle}>{member.name}</Text><View style={styles.pill}><Text style={styles.pillText}>{String(member.attendance_state || 'unknown').replace(/_/g, ' ')}</Text></View></View>
      <Text style={styles.help}>{member.role} · {member.active_job_count || 0} active job(s)</Text>
      {member.attendance?.check_in_at && <Text style={styles.help}>Check-in: {String(member.attendance.check_in_at).replace('T', ' ').slice(0, 19)}</Text>}
      {member.attendance?.check_out_at && <Text style={styles.help}>Check-out: {String(member.attendance.check_out_at).replace('T', ' ').slice(0, 19)}</Text>}
      {member.latest_location && <Button title="Open latest location" kind="secondary" onPress={() => openLocation(member.latest_location)} />}
    </View>)}
    {!loading && !overview?.staff?.length && <View style={styles.empty}><Text style={styles.emptyText}>No staff records for this date.</Text></View>}
  </>;
  else if (section === 'jobs') body = <>
    <Text style={styles.sectionTitle}>Appointments report</Text>
    {(overview?.appointments || []).map(job => <View style={styles.card} key={job.id}>
      <View style={styles.rowBetween}><Text style={styles.jobTime}>{job.date} · {String(job.time || '').slice(0, 5)}</Text><View style={styles.pill}><Text style={styles.pillText}>{stateLabel(stateFor(job))}</Text></View></View>
      <Text style={styles.cardTitle}>{job.name || 'Customer'}</Text><Text style={styles.help}>{job.service || 'Service'} · {(job as any).staff_name || 'Unassigned'}</Text>
      <Text style={styles.address}>{job.service_address || 'Address not provided'}</Text>
    </View>)}
    {!loading && !overview?.appointments?.length && <View style={styles.empty}><Text style={styles.emptyText}>No appointments for this date.</Text></View>}
  </>;
  else if (section === 'leave') body = <>
    <Text style={styles.sectionTitle}>Pending leave approvals</Text>
    {(overview?.pending_leave || []).map(row => <AdminLeaveCard key={row.id} row={row} onUpdated={refresh} />)}
    {!loading && !overview?.pending_leave?.length && <View style={styles.empty}><Text style={styles.emptyText}>No leave requests awaiting approval.</Text></View>}
  </>;
  else body = <>
    <View style={styles.adminHero}><Text style={styles.shiftLabel}>OPERATIONS STATUS</Text><Text style={styles.shiftState}>{summary?.active_jobs || 0} jobs active</Text><Text style={styles.shiftHint}>{summary?.tracking_jobs || 0} sharing live location · {summary?.checked_in || 0} staff checked in</Text></View>
    <View style={styles.adminMetricGrid}>
      <AdminMetric value={summary?.pending_jobs || 0} label="Pending jobs" /><AdminMetric value={summary?.completed_jobs || 0} label="Completed" />
      <AdminMetric value={summary?.staff_total || 0} label="Total staff" /><AdminMetric value={summary?.absent || 0} label="Absent" />
      <AdminMetric value={summary?.on_leave || 0} label="On leave" /><AdminMetric value={summary?.pending_leave || 0} label="Leave approvals" />
    </View>
    <Text style={styles.sectionTitle}>Active field jobs</Text>
    {(overview?.active_jobs || []).slice(0, 6).map(job => <View style={styles.card} key={job.id}>
      <View style={styles.rowBetween}><Text style={styles.cardTitle}>{job.staff_name || 'Unassigned'}</Text><View style={styles.pill}><Text style={styles.pillText}>{job.location_state || job.job_state}</Text></View></View>
      <Text style={styles.detailStrong}>{job.name || 'Customer'} · {job.service || 'Service'}</Text><Text style={styles.help}>{String(job.time || '').slice(0, 5)} · {stateLabel(job.job_state || 'booked')}</Text>
      {job.latest_location && <Button title="View live location" kind="secondary" onPress={() => openLocation(job.latest_location)} />}
    </View>)}
    {!loading && !overview?.active_jobs?.length && <View style={styles.empty}><Text style={styles.emptyText}>No active field jobs.</Text></View>}
  </>;
  return <View style={styles.appShell}><SafeAreaView style={styles.safe}><StatusBar style="dark" />
    <View style={styles.homeHeader}><View><Text style={styles.eyebrow}>AIRZEN ADMIN</Text><Text style={styles.homeGreeting}>Operations control</Text><Text style={styles.homeDate}>{session.admin.username}</Text></View><Pressable onPress={signOut}><Text style={styles.signOut}>Sign out</Text></Pressable></View>
    <View style={styles.adminDateBar}><TextInput value={workDate} onChangeText={setWorkDate} keyboardType="numeric" placeholder="YYYY-MM-DD" style={[styles.input, styles.adminDateInput]} /><Button title="Refresh" kind="secondary" onPress={refresh} disabled={loading} /></View>
    <ScrollView contentContainerStyle={styles.homeScroll} refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.green} />}>{body}</ScrollView>
  </SafeAreaView><AdminBottomNav current={section} onSelect={setSection} /></View>;
}

function AppointmentCard({ appointment, onPress }: { appointment: Appointment; onPress: () => void }) {
  const state = stateFor(appointment);
  return <Pressable onPress={onPress} style={({ pressed }) => [styles.card, styles.jobCard, pressed && styles.pressed]}>
    <View style={styles.rowBetween}><Text style={styles.jobTime}>{appointment.date} · {String(appointment.time || '').slice(0, 5)}</Text>
      <View style={styles.pill}><Text style={styles.pillText}>{stateLabel(state)}</Text></View></View>
    <Text style={styles.jobName}>{appointment.name}</Text><Text style={styles.jobService}>{appointment.service}</Text>
    <Text numberOfLines={2} style={styles.address}>{appointment.service_address || 'Address not provided'}</Text>
  </Pressable>;
}

function HomeScreen({ session, onOpen, onNavigate, onSignedOut }: {
  session: MobileSession; onOpen: (appointment: Appointment) => void;
  onNavigate: (section: StaffSection) => void; onSignedOut: () => void;
}) {
  const [jobs, setJobs] = useState<Appointment[]>([]);
  const [attendance, setAttendance] = useState<Attendance | null>(null);
  const [workDate, setWorkDate] = useState('');
  const [leave, setLeave] = useState<LeaveRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const refresh = async () => {
    setLoading(true);
    try {
      const [jobRows, attendanceResult, leaveRows] = await Promise.all([
        listAppointments(), todayAttendance(), listLeaveRequests(),
      ]);
      setJobs(jobRows); setAttendance(attendanceResult.attendance);
      setWorkDate(attendanceResult.work_date); setLeave(leaveRows);
    } catch (error: any) {
      Alert.alert('Unable to refresh home', error.message || 'Please check your connection.');
    } finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, []);
  const activeJobs = useMemo(() => jobs.filter(job => !['completed', 'cancelled', 'no_show', 'rescheduled'].includes(job.status)), [jobs]);
  const todayJobs = useMemo(() => jobs.filter(job => !workDate || job.date === workDate), [jobs, workDate]);
  const nextJob = activeJobs[0] || null;
  const inProgress = activeJobs.filter(job => ['en_route', 'arrived', 'job_started', 'in_progress'].includes(stateFor(job))).length;
  const completedToday = todayJobs.filter(job => job.status === 'completed').length;
  const pendingLeave = leave.filter(row => row.status === 'pending').length;
  const shiftState = !attendance ? 'Not checked in' : attendance.check_out_at ? 'Shift complete' : 'Checked in';
  const shiftHint = !attendance ? 'Check in before starting field work.' : attendance.check_out_at
    ? `Checked out at ${String(attendance.check_out_at).replace('T', ' ').slice(11, 16)}`
    : `Since ${String(attendance.check_in_at).replace('T', ' ').slice(11, 16)}`;
  const signOut = async () => { await stopJobTracking().catch(() => undefined); await logout(); onSignedOut(); };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.homeHeader}>
    <View><Text style={styles.eyebrow}>AIRZEN FIELD SERVICE</Text><Text style={styles.homeGreeting}>Good day, {session.staff.name.split(' ')[0]}</Text>
      <Text style={styles.homeDate}>{formatWorkDate(workDate)}</Text></View>
    <Pressable accessibilityRole="button" onPress={signOut} style={styles.profileButton}><Text style={styles.profileInitial}>{session.staff.name.slice(0, 1).toUpperCase()}</Text></Pressable>
  </View><ScrollView contentContainerStyle={styles.homeScroll} refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.green} />}>
    <Pressable onPress={() => onNavigate('attendance')} style={[styles.shiftCard, attendance && !attendance.check_out_at && styles.shiftCardActive]}>
      <View><Text style={styles.shiftLabel}>TODAY'S ATTENDANCE</Text><Text style={styles.shiftState}>{shiftState}</Text><Text style={styles.shiftHint}>{shiftHint}</Text></View>
      <View style={styles.shiftAction}><Text style={styles.shiftActionText}>{attendance && !attendance.check_out_at ? 'Open' : attendance?.check_out_at ? 'View' : 'Check in'}</Text></View>
    </Pressable>
    <View style={styles.metricRow}>
      <View style={styles.metricCard}><Text style={styles.metricValue}>{activeJobs.length}</Text><Text style={styles.metricLabel}>Open jobs</Text></View>
      <View style={styles.metricCard}><Text style={styles.metricValue}>{inProgress}</Text><Text style={styles.metricLabel}>Active now</Text></View>
      <View style={styles.metricCard}><Text style={styles.metricValue}>{completedToday}</Text><Text style={styles.metricLabel}>Done today</Text></View>
    </View>
    <Text style={styles.sectionTitle}>Next action</Text>
    {nextJob ? <AppointmentCard appointment={nextJob} onPress={() => onOpen(nextJob)} />
      : <View style={styles.empty}><Text style={styles.emptyTitle}>You are all caught up</Text><Text style={styles.emptyText}>No active job is currently assigned.</Text></View>}
    <Text style={[styles.sectionTitle, { marginTop: 8 }]}>Quick actions</Text>
    <View style={styles.quickGrid}>
      <QuickTile icon="▣" title="My jobs" detail={`${activeJobs.length} active assignment${activeJobs.length === 1 ? '' : 's'}`} onPress={() => onNavigate('jobs')} />
      <QuickTile icon="◷" title="Attendance" detail={shiftState} onPress={() => onNavigate('attendance')} />
      <QuickTile icon="✦" title="Leave" detail={pendingLeave ? `${pendingLeave} awaiting review` : 'Request or check status'} onPress={() => onNavigate('leave')} />
      <QuickTile icon="↻" title="Refresh" detail="Sync latest updates" onPress={refresh} />
    </View>
    <View style={styles.supportCard}><Text style={styles.supportTitle}>Need help?</Text><Text style={styles.help}>Contact your AirZen dispatcher if a job, address, customer start code, or payment record does not look correct.</Text></View>
  </ScrollView></SafeAreaView>;
}

function JobsScreen({ session, onOpen, onSignedOut }: {
  session: MobileSession; onOpen: (appointment: Appointment) => void; onSignedOut: () => void;
}) {
  const [jobs, setJobs] = useState<Appointment[]>([]);
  const [loading, setLoading] = useState(true);
  const refresh = async () => {
    setLoading(true);
    try { setJobs(await listAppointments()); }
    catch (error: any) { Alert.alert('Unable to load jobs', error.message || 'Please check your connection.'); }
    finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, []);
  const active = useMemo(() => jobs.filter(job => !['completed', 'cancelled', 'no_show', 'rescheduled'].includes(job.status)), [jobs]);
  const history = useMemo(() => jobs.filter(job => !active.includes(job)), [jobs, active]);
  const signOut = async () => { await stopJobTracking().catch(() => undefined); await logout(); onSignedOut(); };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.header}>
    <View><Text style={styles.eyebrow}>FIELD SERVICE</Text><Text style={styles.headerTitle}>Hello, {session.staff.name}</Text></View>
    <Pressable onPress={signOut}><Text style={styles.signOut}>Sign out</Text></Pressable>
  </View><ScrollView contentContainerStyle={styles.scroll} refreshControl={<RefreshControl refreshing={loading} onRefresh={refresh} tintColor={colors.green} />}>
    <Text style={styles.sectionTitle}>Assigned jobs</Text>
    {!loading && !active.length && <View style={styles.empty}><Text style={styles.emptyText}>No active jobs assigned.</Text></View>}
    {active.map(job => <AppointmentCard key={job.id} appointment={job} onPress={() => onOpen(job)} />)}
    {!!history.length && <Text style={[styles.sectionTitle, { marginTop: 22 }]}>Recent history</Text>}
    {history.slice(0, 10).map(job => <AppointmentCard key={job.id} appointment={job} onPress={() => onOpen(job)} />)}
  </ScrollView></SafeAreaView>;
}

function AttendanceScreen() {
  const [record, setRecord] = useState<Attendance | null>(null);
  const [workDate, setWorkDate] = useState('');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try { const result = await todayAttendance(); setRecord(result.attendance); setWorkDate(result.work_date); }
    catch (error: any) { Alert.alert('Attendance unavailable', error.message || 'Please try again.'); }
    finally { setBusy(false); }
  };
  useEffect(() => { refresh(); }, []);
  const act = async (action: 'in' | 'out') => {
    setBusy(true);
    try {
      const coords = await currentCoordinates();
      const result = action === 'in' ? await checkIn(coords, notes) : await checkOut(coords, notes);
      setRecord(result.attendance); setWorkDate(result.work_date);
      Alert.alert('Attendance saved', action === 'in' ? 'You are checked in.' : 'You are checked out.');
    } catch (error: any) { Alert.alert('Attendance not saved', error.message || 'Please try again.'); }
    finally { setBusy(false); }
  };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.header}>
    <View><Text style={styles.eyebrow}>MY SHIFT</Text><Text style={styles.headerTitle}>Attendance</Text></View>
  </View><ScrollView contentContainerStyle={styles.scroll}>
    <View style={styles.card}><Text style={styles.cardTitle}>{workDate || 'Today'}</Text>
      <Text style={styles.help}>{record?.check_in_at ? `Checked in: ${String(record.check_in_at).replace('T', ' ').slice(0, 19)}` : 'Not checked in yet.'}</Text>
      <Text style={styles.help}>{record?.check_out_at ? `Checked out: ${String(record.check_out_at).replace('T', ' ').slice(0, 19)}` : record ? 'Currently checked in.' : ''}</Text>
      <Field label="Note (optional)" value={notes} onChangeText={setNotes} placeholder="Shift or attendance note" />
      {!record && <Button title="Check in with location" onPress={() => act('in')} disabled={busy} />}
      {record && !record.check_out_at && <Button title="Check out with location" onPress={() => act('out')} disabled={busy} kind="danger" />}
      {record?.check_out_at && <View style={[styles.card, styles.successCard]}><Text style={styles.help}>Today’s attendance is complete.</Text></View>}
    </View>{busy && <ActivityIndicator size="large" color={colors.green} />}
  </ScrollView></SafeAreaView>;
}

function LeaveScreen() {
  const [rows, setRows] = useState<LeaveRequest[]>([]);
  const [leaveType, setLeaveType] = useState<LeaveRequest['leave_type']>('annual');
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const refresh = async () => { setBusy(true); try { setRows(await listLeaveRequests()); } catch (error: any) { Alert.alert('Unable to load leave', error.message); } finally { setBusy(false); } };
  useEffect(() => { refresh(); }, []);
  const submit = async () => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(startDate) || !/^\d{4}-\d{2}-\d{2}$/.test(endDate) || reason.trim().length < 3) {
      Alert.alert('Check request', 'Enter start/end dates as YYYY-MM-DD and a reason.'); return;
    }
    setBusy(true);
    try { await createLeaveRequest({ leave_type: leaveType, start_date: startDate, end_date: endDate, reason: reason.trim() }); setReason(''); await refresh(); Alert.alert('Leave submitted', 'Your manager can now approve or decline it in CRM.'); }
    catch (error: any) { Alert.alert('Leave not submitted', error.message || 'Please try again.'); }
    finally { setBusy(false); }
  };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.header}>
    <View><Text style={styles.eyebrow}>TIME OFF</Text><Text style={styles.headerTitle}>Leave requests</Text></View>
  </View><ScrollView contentContainerStyle={styles.scroll}>
    <View style={styles.card}><Text style={styles.cardTitle}>Request leave</Text>
      <Text style={styles.label}>Leave type</Text><View style={styles.choiceRow}>{(['annual', 'medical', 'emergency', 'unpaid', 'other'] as const).map(value => <Pressable key={value} onPress={() => setLeaveType(value)} style={[styles.choice, leaveType === value && styles.choiceActive]}><Text style={leaveType === value ? styles.choiceTextActive : styles.choiceText}>{value}</Text></Pressable>)}</View>
      <Field label="Start date" value={startDate} onChangeText={setStartDate} placeholder="YYYY-MM-DD" />
      <Field label="End date" value={endDate} onChangeText={setEndDate} placeholder="YYYY-MM-DD" />
      <Field label="Reason" value={reason} onChangeText={setReason} placeholder="Why you need leave" />
      <Button title="Submit leave request" onPress={submit} disabled={busy} />
    </View><Text style={styles.sectionTitle}>My requests</Text>
    {rows.map(row => <View style={styles.card} key={row.id}><View style={styles.rowBetween}><Text style={styles.cardTitle}>{row.leave_type}</Text><View style={styles.pill}><Text style={styles.pillText}>{row.status}</Text></View></View><Text style={styles.detailStrong}>{row.start_date} to {row.end_date}</Text><Text style={styles.help}>{row.reason}</Text>{!!row.admin_note && <Text style={styles.help}>Manager: {row.admin_note}</Text>}</View>)}
    {!busy && !rows.length && <View style={styles.empty}><Text style={styles.emptyText}>No leave requests yet.</Text></View>}
  </ScrollView></SafeAreaView>;
}

function JobScreen({ initial, onBack }: { initial: Appointment; onBack: () => void }) {
  const [appointment, setAppointment] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [startCode, setStartCode] = useState('');
  const [amount, setAmount] = useState('');
  const [method, setMethod] = useState('cash');
  const [reference, setReference] = useState('');
  const [notes, setNotes] = useState('');
  const [paymentProof, setPaymentProof] = useState<EvidenceAsset | null>(null);
  const refresh = async () => { const result = await appointmentDetail(appointment.id); setAppointment(result.appointment); };
  const perform = async (action: () => Promise<any>, success: string) => {
    setBusy(true);
    try { await action(); await refresh(); Alert.alert('Done', success); }
    catch (error: any) { Alert.alert('Action blocked', error.message || 'Please try again.'); }
    finally { setBusy(false); }
  };
  const openNavigation = () => {
    const target = appointment.location_latitude != null && appointment.location_longitude != null
      ? `${appointment.location_latitude},${appointment.location_longitude}` : encodeURIComponent(appointment.service_address || '');
    Linking.openURL(`https://www.google.com/maps/dir/?api=1&destination=${target}`);
  };
  const startTravel = () => Alert.alert('Share live job location?',
    'AirZen will share your location with dispatch only during this assigned job. Tracking stops when you complete it.', [
      { text: 'Not now', style: 'cancel' }, { text: 'Allow and start', onPress: () => perform(async () => {
        const coords = await currentCoordinates(); await enRoute(appointment.id, coords); await startJobTracking(appointment.id);
      }, 'Travel started. Dispatch can now see your progress.') },
    ]);
  const confirmArrival = async () => {
    const permission = await ImagePicker.requestCameraPermissionsAsync();
    if (!permission.granted) { Alert.alert('Camera required', 'Allow camera access to take the arrival selfie.'); return; }
    const photo = await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], cameraType: ImagePicker.CameraType.front, quality: 0.75, allowsEditing: false });
    if (photo.canceled || !photo.assets?.[0]) return;
    const asset = photo.assets[0];
    await perform(async () => { const coords = await currentCoordinates(); await arrive(appointment.id,
      { uri: asset.uri, mimeType: asset.mimeType, fileName: asset.fileName }, coords); },
    'Arrival verified. Ask the customer for the service start code sent when the booking was confirmed.');
  };
  const verifyCustomerStartCode = () => {
    if (!/^\d{6}$/.test(startCode)) { Alert.alert('Start code required', 'Enter the six-digit service start code from the customer booking confirmation.'); return; }
    perform(() => verifyStartCode(appointment.id, startCode), 'Customer start code verified. The job is now in progress.');
  };
  const capturePayment = async () => {
    const permission = await ImagePicker.requestCameraPermissionsAsync();
    if (!permission.granted) { Alert.alert('Camera required', 'Allow camera access to capture payment proof.'); return; }
    const result = await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 0.8, allowsEditing: false });
    if (!result.canceled && result.assets?.[0]) { const asset = result.assets[0]; setPaymentProof({ uri: asset.uri, mimeType: asset.mimeType, fileName: asset.fileName }); }
  };
  const finish = () => {
    if (!paymentProof || !amount.trim() || !method.trim()) {
      Alert.alert('Payment proof required', 'Add the amount, payment method, and proof photo before closing the job.'); return;
    }
    Alert.alert('Close this service?', 'This marks the CRM service completed and notifies the customer.', [
      { text: 'Review', style: 'cancel' }, { text: 'Complete job', onPress: () => perform(async () => {
        const coords = await currentCoordinates(); await completeJob(appointment.id, paymentProof,
          { amount, method, reference, notes }, coords); await stopJobTracking();
      }, 'Service completed. CRM closure and customer notification are queued.') },
    ]);
  };
  const state = stateFor(appointment);
  const canTravel = ['booked', 'confirmed'].includes(appointment.status) && state === 'booked';
  const canArrive = ['booked', 'confirmed'].includes(appointment.status) && state === 'en_route';
  const canStartCode = ['booked', 'confirmed'].includes(appointment.status) && state === 'arrived';
  const canComplete = appointment.status === 'in_progress' || state === 'job_started';
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.header}>
    <Pressable onPress={onBack}><Text style={styles.back}>‹ Jobs</Text></Pressable><View style={styles.pill}><Text style={styles.pillText}>{stateLabel(state)}</Text></View>
  </View><ScrollView contentContainerStyle={styles.scroll}>
    <View style={styles.card}><Text style={styles.jobName}>{appointment.name}</Text><Text style={styles.jobService}>{appointment.service}</Text>
      <Text style={styles.detailStrong}>{appointment.date} · {String(appointment.time).slice(0, 5)}</Text><Text style={styles.address}>{appointment.service_address || 'Address not provided'}</Text>
      <View style={styles.buttonRow}><Button title="Navigate" kind="secondary" onPress={openNavigation} /><Button title="Call customer" kind="secondary" onPress={() => Linking.openURL(`tel:${appointment.phone}`)} /></View>
    </View>
    <Text style={styles.sectionTitle}>Job actions</Text>
    {canTravel && <Button title="Start travel & live tracking" onPress={startTravel} disabled={busy} />}
    {canArrive && <Button title="Arrived — take selfie" onPress={confirmArrival} disabled={busy} />}
    {canStartCode && <View style={styles.card}><Text style={styles.cardTitle}>Customer service start code</Text><Text style={styles.help}>Ask the customer for the six-digit code included in their booking confirmation. Never start without it.</Text>
      <Field label="Start code" value={startCode} onChangeText={value => setStartCode(value.replace(/\D/g, '').slice(0, 6))} keyboardType="numeric" /><Button title="Verify code & start job" onPress={verifyCustomerStartCode} disabled={busy} /></View>}
    {canComplete && <View style={styles.card}><Text style={styles.cardTitle}>Complete service</Text>
      <Field label="Payment amount" value={amount} onChangeText={setAmount} keyboardType="decimal-pad" placeholder="0.00" />
      <Field label="Payment method" value={method} onChangeText={setMethod} placeholder="cash / PayNow / card" />
      <Field label="Reference (optional)" value={reference} onChangeText={setReference} /><Field label="Completion notes" value={notes} onChangeText={setNotes} />
      <Button title={paymentProof ? 'Payment proof captured ✓' : 'Take payment proof photo'} kind="secondary" onPress={capturePayment} disabled={busy} />
      <Button title="Complete & close job" onPress={finish} disabled={busy} /></View>}
    {state === 'completed' && <View style={[styles.card, styles.successCard]}><Text style={styles.cardTitle}>Service completed</Text>
      <Text style={styles.help}>Location tracking is off. The CRM timeline contains the audit trail and evidence.</Text></View>}
    {busy && <ActivityIndicator size="large" color={colors.green} style={{ marginTop: 20 }} />}
  </ScrollView></SafeAreaView>;
}

function StaffApp() {
  const [session, setSession] = useState<AppSession | null>(null);
  const [selected, setSelected] = useState<Appointment | null>(null);
  const [section, setSection] = useState<StaffSection>('home');
  const [restoring, setRestoring] = useState(true);
  useEffect(() => { loadSession().then(setSession).finally(() => setRestoring(false)); }, []);
  if (restoring) return <SafeAreaView style={[styles.safe, styles.center]}><ActivityIndicator size="large" color={colors.green} /></SafeAreaView>;
  if (!session) return <LoginScreen onLogin={setSession} />;
  if (session.role === 'admin') return <AdminWorkspace session={session} onSignedOut={() => setSession(null)} />;
  const staffSession = session as MobileSession;
  const navigate = (next: StaffSection) => { setSelected(null); setSection(next); };
  let content: React.ReactNode;
  if (selected) content = <JobScreen initial={selected} onBack={() => setSelected(null)} />;
  else if (section === 'attendance') content = <AttendanceScreen />;
  else if (section === 'leave') content = <LeaveScreen />;
  else if (section === 'jobs') content = <JobsScreen session={staffSession} onOpen={setSelected} onSignedOut={() => setSession(null)} />;
  else content = <HomeScreen session={staffSession} onOpen={setSelected} onNavigate={navigate} onSignedOut={() => setSession(null)} />;
  return <View style={styles.appShell}><View style={styles.appContent}>{content}</View>
    <BottomNav current={selected ? 'jobs' : section} onSelect={navigate} />
  </View>;
}

export default function App() {
  return <SafeAreaProvider initialMetrics={initialWindowMetrics}><StaffApp /></SafeAreaProvider>;
}

const styles = StyleSheet.create({
  appShell: { flex: 1, backgroundColor: colors.background }, appContent: { flex: 1 },
  safe: { flex: 1, backgroundColor: colors.background }, center: { alignItems: 'center', justifyContent: 'center' },
  loginWrap: { flex: 1, justifyContent: 'center', padding: 24 },
  loginRoleRow: { flexDirection: 'row', backgroundColor: '#E7EFEB', borderRadius: 14, padding: 4, marginBottom: 16 },
  loginRole: { flex: 1, minHeight: 44, alignItems: 'center', justifyContent: 'center', borderRadius: 11 },
  loginRoleActive: { backgroundColor: colors.surface },
  loginRoleText: { color: colors.muted, fontWeight: '700' },
  loginRoleTextActive: { color: colors.green, fontWeight: '900' },
  brandMark: { width: 58, height: 58, borderRadius: 18, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center', alignSelf: 'center' },
  brandMarkText: { color: '#fff', fontSize: 30, fontWeight: '800' }, title: { fontSize: 30, fontWeight: '800', color: colors.ink, textAlign: 'center', marginTop: 14 },
  subtitle: { fontSize: 15, color: colors.muted, textAlign: 'center', marginBottom: 26 }, privacy: { color: colors.muted, fontSize: 12, textAlign: 'center', lineHeight: 18, marginTop: 18 },
  header: { paddingHorizontal: 20, paddingVertical: 16, backgroundColor: colors.surface, borderBottomWidth: 1, borderBottomColor: colors.border, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  eyebrow: { color: colors.green, fontSize: 11, fontWeight: '800', letterSpacing: 1.6 }, headerTitle: { color: colors.ink, fontSize: 21, fontWeight: '800' },
  signOut: { color: colors.danger, fontWeight: '700' }, back: { color: colors.green, fontWeight: '800', fontSize: 17 }, scroll: { padding: 18, paddingBottom: 48 },
  homeHeader: { paddingHorizontal: 20, paddingTop: 17, paddingBottom: 15, backgroundColor: colors.surface, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  homeGreeting: { color: colors.ink, fontSize: 25, fontWeight: '800', marginTop: 2 }, homeDate: { color: colors.muted, fontSize: 13, marginTop: 3 },
  homeScroll: { padding: 18, paddingBottom: 42 }, profileButton: { width: 44, height: 44, borderRadius: 15, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
  profileInitial: { color: colors.green, fontWeight: '900', fontSize: 18 },
  shiftCard: { backgroundColor: colors.ink, borderRadius: 20, padding: 18, marginBottom: 14, flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  shiftCardActive: { backgroundColor: colors.green }, shiftLabel: { color: '#B8D0C8', fontSize: 10, fontWeight: '800', letterSpacing: 1.3 },
  shiftState: { color: '#FFFFFF', fontSize: 22, fontWeight: '800', marginTop: 4 }, shiftHint: { color: '#D8E7E2', fontSize: 12, marginTop: 4, maxWidth: 220 },
  shiftAction: { backgroundColor: 'rgba(255,255,255,.16)', paddingHorizontal: 13, paddingVertical: 9, borderRadius: 999 }, shiftActionText: { color: '#FFFFFF', fontWeight: '800', fontSize: 12 },
  metricRow: { flexDirection: 'row', gap: 9, marginBottom: 22 }, metricCard: { flex: 1, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 13 },
  adminHero: { backgroundColor: colors.ink, borderRadius: 20, padding: 20, marginBottom: 16 },
  adminMetricGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 10, marginBottom: 22 },
  adminMetric: { width: '48%', backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 15 },
  adminDateBar: { paddingHorizontal: 18, paddingVertical: 10, backgroundColor: colors.surface, borderBottomWidth: 1, borderBottomColor: colors.border, flexDirection: 'row', gap: 10, alignItems: 'center' },
  adminDateInput: { flex: 1, minHeight: 46 },
  metricValue: { color: colors.ink, fontSize: 23, fontWeight: '900' }, metricLabel: { color: colors.muted, fontSize: 11, fontWeight: '700', marginTop: 3 },
  quickGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 }, quickTile: { width: '48%', minHeight: 128, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 18, padding: 15 },
  quickIcon: { width: 34, height: 34, borderRadius: 11, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center', marginBottom: 12 }, quickIconText: { color: colors.green, fontSize: 18, fontWeight: '900' },
  quickTitle: { color: colors.ink, fontSize: 15, fontWeight: '800' }, quickDetail: { color: colors.muted, fontSize: 11, lineHeight: 16, marginTop: 4 },
  supportCard: { backgroundColor: '#EAF1FF', borderRadius: 18, padding: 16, marginTop: 18 }, supportTitle: { color: '#254B82', fontSize: 16, fontWeight: '800' },
  sectionTitle: { color: colors.ink, fontSize: 18, fontWeight: '800', marginBottom: 12 }, card: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 18, padding: 17, marginBottom: 14 },
  successCard: { backgroundColor: colors.greenSoft, borderColor: '#B7DFC9' }, jobCard: { gap: 5 }, pressed: { opacity: 0.75 },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 }, jobTime: { color: colors.green, fontSize: 13, fontWeight: '800' },
  jobName: { color: colors.ink, fontSize: 22, fontWeight: '800', marginTop: 4 }, jobService: { color: colors.muted, fontSize: 15, fontWeight: '600' },
  detailStrong: { color: colors.ink, fontSize: 16, fontWeight: '700', marginTop: 12 }, address: { color: colors.muted, fontSize: 14, lineHeight: 20, marginTop: 5 },
  pill: { backgroundColor: colors.greenSoft, paddingVertical: 6, paddingHorizontal: 10, borderRadius: 999 }, pillText: { color: colors.green, fontWeight: '800', fontSize: 11, textTransform: 'uppercase' },
  field: { marginBottom: 13 }, label: { color: colors.ink, fontSize: 13, fontWeight: '700', marginBottom: 6 },
  input: { backgroundColor: '#FAFCFB', borderWidth: 1, borderColor: colors.border, borderRadius: 12, color: colors.ink, fontSize: 16, minHeight: 50, paddingHorizontal: 14 },
  button: { minHeight: 50, borderRadius: 13, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 15, marginTop: 9, flexGrow: 1 },
  buttonSecondary: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.green }, buttonDanger: { backgroundColor: colors.danger }, buttonMuted: { opacity: 0.55 },
  buttonText: { color: '#fff', fontWeight: '800', fontSize: 15 }, buttonSecondaryText: { color: colors.green }, buttonRow: { flexDirection: 'row', gap: 10, marginTop: 14 },
  bottomNav: { minHeight: 68, paddingHorizontal: 8, paddingTop: 7, backgroundColor: colors.surface, borderTopWidth: 1, borderTopColor: colors.border, flexDirection: 'row', elevation: 12, zIndex: 20 },
  bottomNavItem: { flex: 1, minHeight: 56, alignItems: 'center', justifyContent: 'center', position: 'relative' }, bottomNavIcon: { color: '#8A9993', fontSize: 20, lineHeight: 22 },
  bottomNavIconActive: { color: colors.green }, bottomNavLabel: { color: '#84918C', fontSize: 10, fontWeight: '700', marginTop: 3 }, bottomNavLabelActive: { color: colors.green, fontWeight: '900' },
  bottomNavMarker: { position: 'absolute', top: -7, width: 28, height: 3, borderBottomLeftRadius: 3, borderBottomRightRadius: 3, backgroundColor: colors.green },
  choiceRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 7, marginBottom: 14, marginTop: 7 },
  choice: { borderWidth: 1, borderColor: colors.border, borderRadius: 999, paddingHorizontal: 11, paddingVertical: 8 },
  choiceActive: { backgroundColor: colors.green, borderColor: colors.green }, choiceText: { color: colors.ink, textTransform: 'capitalize' }, choiceTextActive: { color: '#fff', fontWeight: '800', textTransform: 'capitalize' },
  cardTitle: { color: colors.ink, fontSize: 18, fontWeight: '800', marginBottom: 6 }, help: { color: colors.muted, fontSize: 13, lineHeight: 19, marginBottom: 12 },
  empty: { padding: 30, borderWidth: 1, borderStyle: 'dashed', borderColor: colors.border, borderRadius: 16, alignItems: 'center' }, emptyTitle: { color: colors.ink, fontSize: 16, fontWeight: '800', marginBottom: 5 }, emptyText: { color: colors.muted },
});
