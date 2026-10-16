import { StatusBar } from 'expo-status-bar';
import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Modal,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import {
  adminDecideLeave,
  adminLogin,
  adminOperationsOverview,
  createLeaveRequest,
  getAppointmentStats,
  getRevenueSummary,
  getSelectedBranch,
  listAppointments,
  listLeaveRequests,
  loadSession,
  login,
  logout,
  quickCheckIn,
  quickCheckOut,
  setSelectedBranch,
  todayAttendance,
  updateAppointmentStatus,
} from './src/api';
import type {
  AdminOperationsOverview,
  AdminSession,
  AppSession,
  Appointment,
  AppointmentStats,
  Attendance,
  ClinicBranch,
  LeaveRequest,
  LeaveType,
  MobileSession,
} from './src/types';

const BRANCHES: ClinicBranch[] = [
  'ECIL / Kapra (Main Clinic)',
  'AS Rao Nagar Branch',
  'Sainikpuri Branch',
];

const colors = {
  primary: '#0E7490', // Cyan 700 - Professional clinical teal
  primaryDark: '#155E75',
  primarySoft: '#E0F2FE',
  accent: '#0284C7',
  success: '#059669', // Emerald 600
  successSoft: '#D1FAE5',
  warning: '#D97706', // Amber 600
  warningSoft: '#FEF3C7',
  danger: '#DC2626', // Red 600
  dangerSoft: '#FEE2E2',
  ink: '#0F172A', // Slate 900
  muted: '#64748B', // Slate 500
  border: '#E2E8F0', // Slate 200
  surface: '#FFFFFF',
  background: '#F8FAFC', // Slate 50
};

type TabSection = 'appointments' | 'stats' | 'attendance' | 'leave' | 'clinic';

export default function App() {
  return (
    <SafeAreaProvider>
      <MainApp />
    </SafeAreaProvider>
  );
}

function MainApp() {
  const insets = useSafeAreaInsets();
  const [session, setSession] = useState<AppSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [activeTab, setActiveTab] = useState<TabSection>('appointments');
  const [selectedBranch, setBranchState] = useState<ClinicBranch>('ECIL / Kapra (Main Clinic)');

  // Data states
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [stats, setStats] = useState<AppointmentStats | null>(null);
  const [attendance, setAttendance] = useState<Attendance | null>(null);
  const [leaveRequests, setLeaveRequests] = useState<LeaveRequest[]>([]);
  const [adminOverview, setAdminOverview] = useState<AdminOperationsOverview | null>(null);
  const [revenueData, setRevenueData] = useState<any>(null);

  // Modals
  const [showLeaveModal, setShowLeaveModal] = useState(false);
  const [showCheckInModal, setShowCheckInModal] = useState(false);
  const [checkInNotes, setCheckInNotes] = useState('');
  const [leaveType, setLeaveType] = useState<LeaveType>('medical');
  const [leaveStart, setLeaveStart] = useState(new Date().toISOString().slice(0, 10));
  const [leaveEnd, setLeaveEnd] = useState(new Date().toISOString().slice(0, 10));
  const [leaveReason, setLeaveReason] = useState('');

  useEffect(() => {
    (async () => {
      const storedBranch = await getSelectedBranch();
      setBranchState(storedBranch);
      const sess = await loadSession();
      setSession(sess);
      setLoading(false);
    })();
  }, []);

  const loadData = async (branch = selectedBranch) => {
    if (!session) return;
    try {
      const today = new Date().toISOString().slice(0, 10);
      const staffId = session.role === 'staff' ? session.staff.id : undefined;

      const [apptRes, statsRes, attRes, leaveRes] = await Promise.all([
        listAppointments(today, branch),
        getAppointmentStats(branch),
        todayAttendance(staffId),
        listLeaveRequests(staffId),
      ]);

      setAppointments(apptRes);
      setStats(statsRes);
      setAttendance(attRes);
      setLeaveRequests(leaveRes);

      if (session.role === 'admin') {
        const [admRes, revRes] = await Promise.all([
          adminOperationsOverview(today),
          getRevenueSummary(branch),
        ]);
        setAdminOverview(admRes);
        setRevenueData(revRes);
      }
    } catch (err: any) {
      console.warn('Data load error:', err.message);
    }
  };

  useEffect(() => {
    if (session) {
      loadData(selectedBranch);
    }
  }, [session, selectedBranch]);

  const onRefresh = async () => {
    setRefreshing(true);
    await loadData(selectedBranch);
    setRefreshing(false);
  };

  const switchBranch = async (branch: ClinicBranch) => {
    setBranchState(branch);
    await setSelectedBranch(branch);
  };

  const handleQuickCheckIn = async () => {
    if (!session || session.role !== 'staff') return;
    try {
      const today = new Date().toISOString().slice(0, 10);
      await quickCheckIn(session.staff.id, today, checkInNotes);
      setShowCheckInModal(false);
      setCheckInNotes('');
      Alert.alert('Checked In', 'Your duty shift has been recorded successfully.');
      await loadData();
    } catch (err: any) {
      Alert.alert('Check-In Failed', err.message);
    }
  };

  const handleQuickCheckOut = async () => {
    if (!session || session.role !== 'staff') return;
    Alert.alert('Confirm Check-Out', 'Are you sure you want to clock out for today?', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Check-Out',
        style: 'destructive',
        onPress: async () => {
          try {
            const today = new Date().toISOString().slice(0, 10);
            await quickCheckOut(session.staff.id, today);
            Alert.alert('Checked Out', 'Duty shift ended.');
            await loadData();
          } catch (err: any) {
            Alert.alert('Check-Out Failed', err.message);
          }
        },
      },
    ]);
  };

  const handleSubmitLeave = async () => {
    if (!session || session.role !== 'staff') return;
    if (!leaveReason.trim()) {
      Alert.alert('Missing Reason', 'Please provide a reason for the leave request.');
      return;
    }
    try {
      await createLeaveRequest(session.staff.id, leaveType, leaveStart, leaveEnd, leaveReason);
      setShowLeaveModal(false);
      setLeaveReason('');
      Alert.alert('Request Submitted', 'Your leave request is submitted for clinic review.');
      await loadData();
    } catch (err: any) {
      Alert.alert('Failed', err.message);
    }
  };

  const handleUpdateStatus = async (apptId: string, newStatus: string, chair = '') => {
    try {
      await updateAppointmentStatus(apptId, newStatus, `Updated via Staff Mobile`, chair);
      await loadData();
    } catch (err: any) {
      Alert.alert('Status Update Failed', err.message);
    }
  };

  if (loading) {
    return (
      <View style={[styles.centered, { paddingTop: insets.top }]}>
        <ActivityIndicator size="large" color={colors.primary} />
        <Text style={[styles.mutedText, { marginTop: 12 }]}>Loading Smile Dental Staff Portal...</Text>
      </View>
    );
  }

  if (!session) {
    return <LoginView onLogin={(sess) => setSession(sess)} />;
  }

  const staffName = session.role === 'staff' ? session.staff.name : session.admin.name || session.admin.username;
  const staffRole = session.role === 'staff' ? session.staff.role : 'Clinic Administrator';

  return (
    <View style={[styles.container, { paddingTop: Math.max(insets.top, 16) }]}>
      <StatusBar style="dark" />

      {/* Header */}
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.headerTitle}>🦷 Smile Dental</Text>
          <Text style={styles.headerSubtitle}>
            {staffName} • <Text style={{ color: colors.primary, fontWeight: '700' }}>{staffRole}</Text>
          </Text>
        </View>
        <Pressable
          onPress={() => {
            Alert.alert('Sign Out', 'Sign out of your clinic session?', [
              { text: 'Cancel', style: 'cancel' },
              { text: 'Sign Out', style: 'destructive', onPress: async () => { await logout(); setSession(null); } },
            ]);
          }}
          style={styles.logoutButton}
        >
          <Text style={styles.logoutText}>Sign Out</Text>
        </Pressable>
      </View>

      {/* Branch Selector Bar */}
      <View style={styles.branchContainer}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 16, gap: 8 }}>
          {BRANCHES.map((b) => {
            const active = selectedBranch === b;
            return (
              <Pressable
                key={b}
                onPress={() => switchBranch(b)}
                style={[styles.branchPill, active && styles.branchPillActive]}
              >
                <Text style={[styles.branchPillText, active && styles.branchPillTextActive]}>
                  📍 {b.split(' (')[0]}
                </Text>
              </Pressable>
            );
          })}
        </ScrollView>
      </View>

      {/* Content Area */}
      <ScrollView
        style={styles.content}
        contentContainerStyle={{ padding: 16, paddingBottom: insets.bottom + 80 }}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} colors={[colors.primary]} />}
      >
        {activeTab === 'appointments' && (
          <AppointmentsSection
            appointments={appointments}
            onUpdateStatus={handleUpdateStatus}
            branch={selectedBranch}
          />
        )}

        {activeTab === 'stats' && (
          <StatsSection stats={stats} branch={selectedBranch} />
        )}

        {activeTab === 'attendance' && (
          <AttendanceSection
            attendance={attendance}
            onOpenCheckIn={() => setShowCheckInModal(true)}
            onCheckOut={handleQuickCheckOut}
            role={session.role}
          />
        )}

        {activeTab === 'leave' && (
          <LeaveSection
            leaveRequests={leaveRequests}
            onOpenSubmit={() => setShowLeaveModal(true)}
            isAdmin={session.role === 'admin'}
            onApprove={async (id) => { await adminDecideLeave(id, 'approved'); await loadData(); }}
            onDecline={async (id) => { await adminDecideLeave(id, 'declined'); await loadData(); }}
          />
        )}

        {activeTab === 'clinic' && (
          <ClinicOverviewSection
            overview={adminOverview}
            revenue={revenueData}
            branch={selectedBranch}
          />
        )}
      </ScrollView>

      {/* Bottom Navigation */}
      <View style={[styles.bottomNav, { paddingBottom: Math.max(insets.bottom, 8) }]}>
        <NavButton
          icon="📅"
          label="Patients"
          active={activeTab === 'appointments'}
          onPress={() => setActiveTab('appointments')}
          badge={appointments.filter((a) => a.status === 'booked' || a.status === 'in_operatory').length}
        />
        <NavButton
          icon="📊"
          label="Doctor KPIs"
          active={activeTab === 'stats'}
          onPress={() => setActiveTab('stats')}
        />
        <NavButton
          icon="⏱️"
          label="Attendance"
          active={activeTab === 'attendance'}
          onPress={() => setActiveTab('attendance')}
          statusDot={Boolean(attendance?.check_in_at && !attendance?.check_out_at)}
        />
        <NavButton
          icon="🏖️"
          label="Leave"
          active={activeTab === 'leave'}
          onPress={() => setActiveTab('leave')}
        />
        <NavButton
          icon="🏥"
          label="Multi-Branch"
          active={activeTab === 'clinic'}
          onPress={() => setActiveTab('clinic')}
        />
      </View>

      {/* Check-In Modal */}
      <Modal visible={showCheckInModal} transparent animationType="slide">
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>⏱️ Clinic Duty Check-In</Text>
            <Text style={styles.mutedText}>Record start of shift at {selectedBranch}</Text>

            <Text style={[styles.fieldLabel, { marginTop: 14 }]}>Shift Notes / Remarks</Text>
            <TextInput
              style={styles.modalInput}
              placeholder="e.g. Morning Operatory Shift, Chair 1 Ready"
              value={checkInNotes}
              onChangeText={setCheckInNotes}
            />

            <View style={styles.modalActions}>
              <Pressable style={[styles.btn, styles.btnPrimary]} onPress={handleQuickCheckIn}>
                <Text style={styles.btnTextPrimary}>Confirm Check-In</Text>
              </Pressable>
              <Pressable style={[styles.btn, styles.btnSecondary]} onPress={() => setShowCheckInModal(false)}>
                <Text style={styles.btnTextSecondary}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>

      {/* Leave Request Modal */}
      <Modal visible={showLeaveModal} transparent animationType="slide">
        <View style={styles.modalOverlay}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>🏖️ Request Staff Leave</Text>
            <Text style={styles.mutedText}>Submit scheduled leave for doctor or clinic approval</Text>

            <Text style={[styles.fieldLabel, { marginTop: 12 }]}>Leave Type</Text>
            <View style={{ flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
              {(['medical', 'annual', 'emergency', 'casual'] as LeaveType[]).map((t) => (
                <Pressable
                  key={t}
                  onPress={() => setLeaveType(t)}
                  style={[styles.typePill, leaveType === t && styles.typePillActive]}
                >
                  <Text style={[styles.typePillText, leaveType === t && styles.typePillTextActive]}>
                    {t.toUpperCase()}
                  </Text>
                </Pressable>
              ))}
            </View>

            <Text style={[styles.fieldLabel, { marginTop: 12 }]}>Start Date (YYYY-MM-DD)</Text>
            <TextInput style={styles.modalInput} value={leaveStart} onChangeText={setLeaveStart} />

            <Text style={[styles.fieldLabel, { marginTop: 8 }]}>End Date (YYYY-MM-DD)</Text>
            <TextInput style={styles.modalInput} value={leaveEnd} onChangeText={setLeaveEnd} />

            <Text style={[styles.fieldLabel, { marginTop: 8 }]}>Reason for Leave</Text>
            <TextInput
              style={[styles.modalInput, { minHeight: 60 }]}
              multiline
              placeholder="Reason for requesting time off..."
              value={leaveReason}
              onChangeText={setLeaveReason}
            />

            <View style={styles.modalActions}>
              <Pressable style={[styles.btn, styles.btnPrimary]} onPress={handleSubmitLeave}>
                <Text style={styles.btnTextPrimary}>Submit Request</Text>
              </Pressable>
              <Pressable style={[styles.btn, styles.btnSecondary]} onPress={() => setShowLeaveModal(false)}>
                <Text style={styles.btnTextSecondary}>Cancel</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

// ----------------- SUB-COMPONENTS -----------------

function AppointmentsSection({
  appointments,
  onUpdateStatus,
  branch,
}: {
  appointments: Appointment[];
  onUpdateStatus: (id: string, status: string, chair?: string) => void;
  branch: string;
}) {
  return (
    <View style={{ gap: 12 }}>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={styles.sectionHeading}>Today's Patient Queue</Text>
        <Text style={styles.badgeCount}>{appointments.length} Patients</Text>
      </View>

      {!appointments.length ? (
        <View style={styles.emptyCard}>
          <Text style={{ fontSize: 32, marginBottom: 8 }}>🦷</Text>
          <Text style={styles.cardTitle}>No Appointments Today</Text>
          <Text style={styles.mutedText}>No consultations scheduled for {branch} today.</Text>
        </View>
      ) : (
        appointments.map((appt) => {
          const isBooked = appt.status === 'booked';
          const isOperatory = appt.status === 'in_operatory' || appt.status === 'in_progress';
          const isDone = appt.status === 'completed';

          return (
            <View key={appt.id} style={styles.card}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.patientName}>{appt.name}</Text>
                  <Text style={styles.serviceName}>🦷 {appt.service || 'Dental Consultation'}</Text>
                  <Text style={styles.mutedText}>
                    ⏰ {appt.time || 'Today'} • 📞 {appt.phone}
                  </Text>
                  {appt.doctor_name ? (
                    <Text style={[styles.mutedText, { marginTop: 2, color: colors.primary, fontWeight: '600' }]}>
                      👨‍⚕️ {appt.doctor_name}
                    </Text>
                  ) : null}
                </View>

                <View
                  style={[
                    styles.statusBadge,
                    isDone ? styles.badgeSuccess : isOperatory ? styles.badgeWarning : styles.badgeInfo,
                  ]}
                >
                  <Text
                    style={[
                      styles.statusBadgeText,
                      isDone ? styles.textSuccess : isOperatory ? styles.textWarning : styles.textInfo,
                    ]}
                  >
                    {isDone ? 'Completed' : isOperatory ? 'In Operatory' : 'Waiting'}
                  </Text>
                </View>
              </View>

              {/* Chair & Procedure Actions */}
              <View style={styles.cardActionRow}>
                {isBooked && (
                  <>
                    <Pressable
                      style={[styles.actionChip, { backgroundColor: colors.primarySoft }]}
                      onPress={() => onUpdateStatus(appt.id, 'in_operatory', 'Chair 1')}
                    >
                      <Text style={[styles.actionChipText, { color: colors.primaryDark }]}>🪑 Seat Chair 1</Text>
                    </Pressable>
                    <Pressable
                      style={[styles.actionChip, { backgroundColor: colors.primarySoft }]}
                      onPress={() => onUpdateStatus(appt.id, 'in_operatory', 'Chair 2')}
                    >
                      <Text style={[styles.actionChipText, { color: colors.primaryDark }]}>🪑 Seat Chair 2</Text>
                    </Pressable>
                  </>
                )}

                {isOperatory && (
                  <Pressable
                    style={[styles.actionChip, { backgroundColor: colors.successSoft }]}
                    onPress={() => onUpdateStatus(appt.id, 'completed')}
                  >
                    <Text style={[styles.actionChipText, { color: colors.success }]}>✅ Mark Treatment Done</Text>
                  </Pressable>
                )}

                {isDone && (
                  <Text style={[styles.mutedText, { fontSize: 12, fontStyle: 'italic' }]}>
                    Treatment completed for this sitting
                  </Text>
                )}
              </View>
            </View>
          );
        })
      )}
    </View>
  );
}

function StatsSection({ stats, branch }: { stats: AppointmentStats | null; branch: string }) {
  if (!stats) {
    return <ActivityIndicator size="small" color={colors.primary} />;
  }

  return (
    <View style={{ gap: 14 }}>
      <Text style={styles.sectionHeading}>Doctor & Appointment KPIs</Text>
      <Text style={styles.mutedText}>Live consultation metrics for {branch}</Text>

      {/* KPI 4-Grid */}
      <View style={styles.statsGrid}>
        <View style={styles.kpiCard}>
          <Text style={styles.kpiLabel}>📅 Today's Bookings</Text>
          <Text style={styles.kpiValue}>{stats.today}</Text>
          <Text style={styles.kpiFoot}>{stats.date_today}</Text>
        </View>
        <View style={styles.kpiCard}>
          <Text style={styles.kpiLabel}>📆 This Week</Text>
          <Text style={[styles.kpiValue, { color: colors.accent }]}>{stats.this_week}</Text>
          <Text style={styles.kpiFoot}>Mon–Sun</Text>
        </View>
        <View style={styles.kpiCard}>
          <Text style={styles.kpiLabel}>🗓️ This Month</Text>
          <Text style={[styles.kpiValue, { color: colors.primary }]}>{stats.this_month}</Text>
          <Text style={styles.kpiFoot}>Current Month</Text>
        </View>
        <View style={styles.kpiCard}>
          <Text style={styles.kpiLabel}>👥 Total Patients</Text>
          <Text style={[styles.kpiValue, { color: colors.success }]}>{stats.total}</Text>
          <Text style={styles.kpiFoot}>All Time</Text>
        </View>
      </View>

      {/* Doctor Breakdown */}
      {stats.by_doctor && Object.keys(stats.by_doctor).length > 0 && (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>👨‍⚕️ Bookings by Attending Doctor</Text>
          <View style={{ marginTop: 8, gap: 6 }}>
            {Object.entries(stats.by_doctor).map(([doc, cnt]) => (
              <View key={doc} style={styles.breakdownRow}>
                <Text style={[styles.breakdownLabel, { flex: 1 }]}>{doc}</Text>
                <Text style={styles.breakdownVal}>{cnt} patients</Text>
              </View>
            ))}
          </View>
        </View>
      )}

      {/* Branch Breakdown */}
      {stats.by_branch && Object.keys(stats.by_branch).length > 0 && (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>🏥 Multi-Branch Appointments</Text>
          <View style={{ marginTop: 8, gap: 6 }}>
            {Object.entries(stats.by_branch).map(([br, cnt]) => (
              <View key={br} style={styles.breakdownRow}>
                <Text style={[styles.breakdownLabel, { flex: 1 }]}>{br}</Text>
                <Text style={[styles.breakdownVal, { color: colors.primary }]}>{cnt} booked</Text>
              </View>
            ))}
          </View>
        </View>
      )}
    </View>
  );
}

function AttendanceSection({
  attendance,
  onOpenCheckIn,
  onCheckOut,
  role,
}: {
  attendance: Attendance | null;
  onOpenCheckIn: () => void;
  onCheckOut: () => void;
  role: string;
}) {
  const isCheckedIn = Boolean(attendance?.check_in_at && !attendance?.check_out_at);
  const isCheckedOut = Boolean(attendance?.check_out_at);

  return (
    <View style={{ gap: 14 }}>
      <Text style={styles.sectionHeading}>Clinic Shift & Attendance</Text>
      <Text style={styles.mutedText}>Single-tap duty attendance for doctors, nurses, and receptionists.</Text>

      <View style={styles.card}>
        <View style={{ flexDirection: 'row', alignItems: 'center', gap: 12 }}>
          <View
            style={[
              styles.avatarIcon,
              { backgroundColor: isCheckedIn ? colors.successSoft : colors.primarySoft },
            ]}
          >
            <Text style={{ fontSize: 24 }}>{isCheckedIn ? '🟢' : '⏱️'}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={styles.cardTitle}>
              {isCheckedIn
                ? 'Duty Active (Checked-In)'
                : isCheckedOut
                ? 'Shift Completed (Checked-Out)'
                : 'Not Checked In Today'}
            </Text>
            <Text style={styles.mutedText}>
              {attendance?.check_in_at
                ? `Started: ${String(attendance.check_in_at).slice(11, 16)}`
                : 'Tap below to record duty check-in'}
            </Text>
          </View>
        </View>

        {attendance?.notes ? (
          <View style={styles.noteBox}>
            <Text style={styles.noteText}>📝 {attendance.notes}</Text>
          </View>
        ) : null}

        <View style={{ marginTop: 16 }}>
          {!isCheckedIn && !isCheckedOut && (
            <Pressable style={[styles.btn, styles.btnPrimary]} onPress={onOpenCheckIn}>
              <Text style={styles.btnTextPrimary}>🟢 Record Duty Check-In</Text>
            </Pressable>
          )}

          {isCheckedIn && (
            <Pressable style={[styles.btn, styles.btnDanger]} onPress={onCheckOut}>
              <Text style={styles.btnTextPrimary}>🔴 End Shift / Check-Out</Text>
            </Pressable>
          )}

          {isCheckedOut && (
            <Pressable style={[styles.btn, styles.btnSecondary]} onPress={onOpenCheckIn}>
              <Text style={styles.btnTextSecondary}>🔁 Re-open Additional Shift</Text>
            </Pressable>
          )}
        </View>
      </View>
    </View>
  );
}

function LeaveSection({
  leaveRequests,
  onOpenSubmit,
  isAdmin,
  onApprove,
  onDecline,
}: {
  leaveRequests: LeaveRequest[];
  onOpenSubmit: () => void;
  isAdmin: boolean;
  onApprove: (id: string) => void;
  onDecline: (id: string) => void;
}) {
  return (
    <View style={{ gap: 14 }}>
      <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
        <Text style={styles.sectionHeading}>Leave & Time-Off</Text>
        <Pressable style={styles.smallActionBtn} onPress={onOpenSubmit}>
          <Text style={styles.smallActionBtnText}>+ Request Leave</Text>
        </Pressable>
      </View>

      {!leaveRequests.length ? (
        <View style={styles.emptyCard}>
          <Text style={{ fontSize: 32, marginBottom: 8 }}>🏖️</Text>
          <Text style={styles.cardTitle}>No Leave Requests</Text>
          <Text style={styles.mutedText}>No active or past time-off requests found.</Text>
        </View>
      ) : (
        leaveRequests.map((req) => {
          const isPending = req.status === 'pending';
          const isApproved = req.status === 'approved';

          return (
            <View key={req.id} style={styles.card}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between' }}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.cardTitle}>
                    {req.staff_name ? `${req.staff_name} • ` : ''}
                    {req.leave_type.toUpperCase()} LEAVE
                  </Text>
                  <Text style={styles.mutedText}>
                    📅 {req.start_date} to {req.end_date}
                  </Text>
                  <Text style={[styles.mutedText, { marginTop: 4, color: colors.ink }]}>
                    "{req.reason}"
                  </Text>
                </View>

                <View
                  style={[
                    styles.statusBadge,
                    isApproved ? styles.badgeSuccess : isPending ? styles.badgeWarning : styles.badgeDanger,
                  ]}
                >
                  <Text
                    style={[
                      styles.statusBadgeText,
                      isApproved ? styles.textSuccess : isPending ? styles.textWarning : styles.textDanger,
                    ]}
                  >
                    {req.status.toUpperCase()}
                  </Text>
                </View>
              </View>

              {isAdmin && isPending && (
                <View style={{ flexDirection: 'row', gap: 8, marginTop: 12 }}>
                  <Pressable style={[styles.btn, styles.btnSuccess, { flex: 1 }]} onPress={() => onApprove(req.id)}>
                    <Text style={styles.btnTextPrimary}>Approve</Text>
                  </Pressable>
                  <Pressable style={[styles.btn, styles.btnDanger, { flex: 1 }]} onPress={() => onDecline(req.id)}>
                    <Text style={styles.btnTextPrimary}>Decline</Text>
                  </Pressable>
                </View>
              )}
            </View>
          );
        })
      )}
    </View>
  );
}

function ClinicOverviewSection({
  overview,
  revenue,
  branch,
}: {
  overview: AdminOperationsOverview | null;
  revenue: any;
  branch: string;
}) {
  return (
    <View style={{ gap: 14 }}>
      <Text style={styles.sectionHeading}>Multi-Branch Operations & Revenue</Text>
      <Text style={styles.mutedText}>Real-time clinic operations and revenue dashboard</Text>

      {/* Revenue Card */}
      {revenue && (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>💰 {branch} Revenue</Text>
          <View style={{ flexDirection: 'row', gap: 12, marginTop: 10 }}>
            <View style={[styles.kpiCard, { flex: 1 }]}>
              <Text style={styles.kpiLabel}>Total Billed</Text>
              <Text style={[styles.kpiValue, { color: colors.primary }]}>
                ₹{(revenue.total_billed || 0).toLocaleString()}
              </Text>
            </View>
            <View style={[styles.kpiCard, { flex: 1 }]}>
              <Text style={styles.kpiLabel}>Collected</Text>
              <Text style={[styles.kpiValue, { color: colors.success }]}>
                ₹{(revenue.total_collected || 0).toLocaleString()}
              </Text>
            </View>
          </View>
        </View>
      )}

      {/* Staff Presence Overview */}
      {overview && (
        <View style={styles.card}>
          <Text style={styles.cardTitle}>👥 Clinic Staff on Duty</Text>
          <View style={{ marginTop: 8, gap: 8 }}>
            {(overview.staff || []).map((s) => (
              <View key={s.id} style={styles.breakdownRow}>
                <View style={{ flex: 1 }}>
                  <Text style={styles.breakdownLabel}>{s.name}</Text>
                  <Text style={styles.mutedText}>
                    {s.role} • {s.branch_name || 'Main Clinic'}
                  </Text>
                </View>
                <View
                  style={[
                    styles.statusBadge,
                    s.checked_in ? styles.badgeSuccess : styles.badgeDanger,
                  ]}
                >
                  <Text
                    style={[
                      styles.statusBadgeText,
                      s.checked_in ? styles.textSuccess : styles.textDanger,
                    ]}
                  >
                    {s.checked_in ? 'On Duty' : 'Absent'}
                  </Text>
                </View>
              </View>
            ))}
          </View>
        </View>
      )}
    </View>
  );
}

function NavButton({
  icon,
  label,
  active,
  onPress,
  badge,
  statusDot,
}: {
  icon: string;
  label: string;
  active: boolean;
  onPress: () => void;
  badge?: number;
  statusDot?: boolean;
}) {
  return (
    <Pressable onPress={onPress} style={styles.navBtn}>
      <View style={{ position: 'relative' }}>
        <Text style={[styles.navIcon, active && styles.navIconActive]}>{icon}</Text>
        {statusDot && <View style={styles.statusDot} />}
        {Boolean(badge) && (
          <View style={styles.navBadge}>
            <Text style={styles.navBadgeText}>{badge}</Text>
          </View>
        )}
      </View>
      <Text style={[styles.navLabel, active && styles.navLabelActive]}>{label}</Text>
    </Pressable>
  );
}

function LoginView({ onLogin }: { onLogin: (session: AppSession) => void }) {
  const [isAdmin, setIsAdmin] = useState(false);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('SmileDental@2026');
  const [pin, setPin] = useState('123456');
  const [loading, setLoading] = useState(false);

  const handleSignIn = async () => {
    setLoading(true);
    try {
      if (isAdmin) {
        const sess = await adminLogin(username.trim(), password.trim());
        onLogin(sess);
      } else {
        const sess = await login(username.trim(), pin.trim());
        onLogin(sess);
      }
    } catch (err: any) {
      Alert.alert('Login Failed', err.message || 'Invalid credentials');
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={styles.loginContainer}>
      <View style={styles.loginCard}>
        <Text style={{ fontSize: 44, textAlign: 'center', marginBottom: 8 }}>🦷</Text>
        <Text style={styles.loginTitle}>Smile Dental Clinic</Text>
        <Text style={[styles.mutedText, { textAlign: 'center', marginBottom: 20 }]}>
          Staff & Doctor Companion App
        </Text>

        <View style={styles.roleToggle}>
          <Pressable
            style={[styles.roleBtn, !isAdmin && styles.roleBtnActive]}
            onPress={() => setIsAdmin(false)}
          >
            <Text style={[styles.roleBtnText, !isAdmin && styles.roleBtnTextActive]}>Doctor / Staff</Text>
          </Pressable>
          <Pressable
            style={[styles.roleBtn, isAdmin && styles.roleBtnActive]}
            onPress={() => setIsAdmin(true)}
          >
            <Text style={[styles.roleBtnText, isAdmin && styles.roleBtnTextActive]}>Clinic Admin</Text>
          </Pressable>
        </View>

        <Text style={styles.fieldLabel}>Username</Text>
        <TextInput
          style={styles.modalInput}
          value={username}
          onChangeText={setUsername}
          autoCapitalize="none"
          placeholder="e.g. admin or doctor username"
        />

        {isAdmin ? (
          <>
            <Text style={[styles.fieldLabel, { marginTop: 10 }]}>Password</Text>
            <TextInput
              style={styles.modalInput}
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              placeholder="Admin password"
            />
          </>
        ) : (
          <>
            <Text style={[styles.fieldLabel, { marginTop: 10 }]}>6-Digit Staff PIN</Text>
            <TextInput
              style={styles.modalInput}
              value={pin}
              onChangeText={setPin}
              keyboardType="numeric"
              maxLength={6}
              secureTextEntry
              placeholder="6-digit mobile PIN"
            />
          </>
        )}

        <Pressable
          style={[styles.btn, styles.btnPrimary, { marginTop: 20 }]}
          onPress={handleSignIn}
          disabled={loading}
        >
          {loading ? (
            <ActivityIndicator color="#fff" />
          ) : (
            <Text style={styles.btnTextPrimary}>Sign In to Portal</Text>
          )}
        </Pressable>
      </View>
    </View>
  );
}

// ----------------- STYLES -----------------
const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  centered: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: colors.background },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingHorizontal: 16,
    paddingBottom: 10,
  },
  headerTitle: { fontSize: 20, fontWeight: '800', color: colors.ink },
  headerSubtitle: { fontSize: 13, color: colors.muted, marginTop: 2 },
  logoutButton: { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 8, backgroundColor: colors.border },
  logoutText: { fontSize: 12, fontWeight: '600', color: colors.ink },

  branchContainer: { borderBottomWidth: 1, borderBottomColor: colors.border, paddingBottom: 10 },
  branchPill: {
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 20,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
  },
  branchPillActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  branchPillText: { fontSize: 13, fontWeight: '600', color: colors.muted },
  branchPillTextActive: { color: '#FFFFFF' },

  content: { flex: 1 },
  sectionHeading: { fontSize: 17, fontWeight: '700', color: colors.ink },
  mutedText: { fontSize: 13, color: colors.muted },

  card: {
    backgroundColor: colors.surface,
    borderRadius: 14,
    padding: 14,
    borderWidth: 1,
    borderColor: colors.border,
    shadowColor: '#000',
    shadowOpacity: 0.03,
    shadowOffset: { width: 0, height: 2 },
    shadowRadius: 6,
    elevation: 2,
  },
  emptyCard: {
    backgroundColor: colors.surface,
    borderRadius: 14,
    padding: 24,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: colors.border,
  },
  cardTitle: { fontSize: 15, fontWeight: '700', color: colors.ink },
  patientName: { fontSize: 16, fontWeight: '700', color: colors.ink },
  serviceName: { fontSize: 14, fontWeight: '600', color: colors.primary, marginTop: 2 },

  statusBadge: { paddingHorizontal: 8, paddingVertical: 4, borderRadius: 6 },
  badgeSuccess: { backgroundColor: colors.successSoft },
  badgeWarning: { backgroundColor: colors.warningSoft },
  badgeDanger: { backgroundColor: colors.dangerSoft },
  badgeInfo: { backgroundColor: colors.primarySoft },
  statusBadgeText: { fontSize: 11, fontWeight: '700' },
  textSuccess: { color: colors.success },
  textWarning: { color: colors.warning },
  textDanger: { color: colors.danger },
  textInfo: { color: colors.primaryDark },

  badgeCount: {
    fontSize: 12,
    fontWeight: '700',
    backgroundColor: colors.primarySoft,
    color: colors.primaryDark,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 10,
  },

  cardActionRow: { flexDirection: 'row', gap: 8, marginTop: 12, alignItems: 'center' },
  actionChip: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 8 },
  actionChipText: { fontSize: 12, fontWeight: '700' },

  statsGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  kpiCard: {
    flex: 1,
    minWidth: '45%',
    backgroundColor: colors.surface,
    borderRadius: 12,
    padding: 12,
    borderWidth: 1,
    borderColor: colors.border,
  },
  kpiLabel: { fontSize: 12, fontWeight: '600', color: colors.muted },
  kpiValue: { fontSize: 24, fontWeight: '800', color: colors.ink, marginVertical: 4 },
  kpiFoot: { fontSize: 11, color: colors.muted },

  breakdownRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  breakdownLabel: { fontSize: 13, color: colors.ink, fontWeight: '500' },
  breakdownVal: { fontSize: 13, fontWeight: '700', color: colors.ink },

  avatarIcon: { width: 48, height: 48, borderRadius: 24, justifyContent: 'center', alignItems: 'center' },
  noteBox: {
    backgroundColor: colors.background,
    padding: 8,
    borderRadius: 8,
    marginTop: 10,
    borderWidth: 1,
    borderColor: colors.border,
  },
  noteText: { fontSize: 12, color: colors.muted },

  smallActionBtn: {
    backgroundColor: colors.primarySoft,
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 8,
  },
  smallActionBtnText: { fontSize: 12, fontWeight: '700', color: colors.primaryDark },

  bottomNav: {
    position: 'absolute',
    bottom: 0,
    left: 0,
    right: 0,
    flexDirection: 'row',
    backgroundColor: colors.surface,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    paddingTop: 8,
  },
  navBtn: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  navIcon: { fontSize: 20, opacity: 0.5 },
  navIconActive: { opacity: 1 },
  navLabel: { fontSize: 10, color: colors.muted, marginTop: 2 },
  navLabelActive: { color: colors.primary, fontWeight: '700' },
  statusDot: {
    position: 'absolute',
    top: 0,
    right: -4,
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.success,
  },
  navBadge: {
    position: 'absolute',
    top: -2,
    right: -8,
    backgroundColor: colors.danger,
    borderRadius: 8,
    paddingHorizontal: 4,
    paddingVertical: 1,
  },
  navBadgeText: { color: '#fff', fontSize: 9, fontWeight: '700' },

  modalOverlay: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.5)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: 20,
  },
  modalCard: {
    width: '100%',
    backgroundColor: colors.surface,
    borderRadius: 16,
    padding: 20,
    borderWidth: 1,
    borderColor: colors.border,
  },
  modalTitle: { fontSize: 18, fontWeight: '800', color: colors.ink, marginBottom: 4 },
  fieldLabel: { fontSize: 13, fontWeight: '600', color: colors.ink, marginBottom: 4 },
  modalInput: {
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 8,
    fontSize: 14,
    color: colors.ink,
  },
  modalActions: { flexDirection: 'row', gap: 10, marginTop: 18 },

  typePill: {
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 6,
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
  },
  typePillActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  typePillText: { fontSize: 11, fontWeight: '600', color: colors.muted },
  typePillTextActive: { color: '#fff' },

  btn: { paddingVertical: 11, borderRadius: 8, alignItems: 'center', justifyContent: 'center' },
  btnPrimary: { backgroundColor: colors.primary },
  btnSecondary: { backgroundColor: colors.border },
  btnSuccess: { backgroundColor: colors.success },
  btnDanger: { backgroundColor: colors.danger },
  btnTextPrimary: { color: '#FFFFFF', fontWeight: '700', fontSize: 13 },
  btnTextSecondary: { color: colors.ink, fontWeight: '600', fontSize: 13 },

  loginContainer: { flex: 1, backgroundColor: colors.background, justifyContent: 'center', padding: 20 },
  loginCard: {
    backgroundColor: colors.surface,
    borderRadius: 16,
    padding: 24,
    borderWidth: 1,
    borderColor: colors.border,
  },
  loginTitle: { fontSize: 22, fontWeight: '800', color: colors.ink, textAlign: 'center' },
  roleToggle: {
    flexDirection: 'row',
    backgroundColor: colors.background,
    borderRadius: 8,
    padding: 3,
    marginBottom: 16,
  },
  roleBtn: { flex: 1, paddingVertical: 8, borderRadius: 6, alignItems: 'center' },
  roleBtnActive: { backgroundColor: colors.surface, elevation: 1 },
  roleBtnText: { fontSize: 12, fontWeight: '600', color: colors.muted },
  roleBtnTextActive: { color: colors.primary, fontWeight: '700' },
});
