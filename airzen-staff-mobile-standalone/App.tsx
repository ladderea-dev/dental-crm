import { StatusBar } from 'expo-status-bar';
import * as ImagePicker from 'expo-image-picker';
import React, { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator, Alert, Linking, Pressable, RefreshControl, SafeAreaView,
  ScrollView, StyleSheet, Text, TextInput, View,
} from 'react-native';

import {
  appointmentDetail, arrive, completeJob, enRoute, listAppointments,
  loadSession, login, logout, verifyOtp,
} from './src/api';
import { currentCoordinates, startJobTracking, stopJobTracking } from './src/locationTask';
import type { Appointment, EvidenceAsset, MobileSession } from './src/types';

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
  return ({ booked: 'Assigned', en_route: 'Travelling', arrived: 'Awaiting customer OTP',
    job_started: 'In progress', in_progress: 'In progress', completed: 'Completed' } as Record<string, string>)[state]
    || state.replace(/_/g, ' ');
}

function LoginScreen({ onLogin }: { onLogin: (session: MobileSession) => void }) {
  const [username, setUsername] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/.test(username.trim()) || !/^\d{6}$/.test(pin)) {
      Alert.alert('Check login', 'Enter your staff username and six-digit PIN.'); return;
    }
    setBusy(true);
    try { onLogin(await login(username.trim().toLowerCase(), pin)); }
    catch (error: any) { Alert.alert('Login failed', error.message || 'Please check your details.'); }
    finally { setBusy(false); }
  };
  return <SafeAreaView style={styles.safe}><StatusBar style="dark" /><View style={styles.loginWrap}>
    <View style={styles.brandMark}><Text style={styles.brandMarkText}>A</Text></View>
    <Text style={styles.title}>AirZen Staff</Text><Text style={styles.subtitle}>Secure field-service workspace</Text>
    <View style={styles.card}>
      <Field label="Staff username" value={username} onChangeText={value => setUsername(value.replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64))} placeholder="e.g. arun.tech" />
      <Field label="Six-digit PIN" value={pin} onChangeText={value => setPin(value.replace(/\D/g, '').slice(0, 6))} secureTextEntry keyboardType="numeric" />
      <Button title={busy ? 'Signing in…' : 'Sign in'} onPress={submit} disabled={busy} />
    </View>
    <Text style={styles.privacy}>Only use your own staff account. Customer data and job evidence are audited.</Text>
  </View></SafeAreaView>;
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

function JobScreen({ initial, onBack }: { initial: Appointment; onBack: () => void }) {
  const [appointment, setAppointment] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [otp, setOtp] = useState('');
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
    'Arrival verified. The customer OTP has been queued on WhatsApp.');
  };
  const verifyCustomerOtp = () => {
    if (!/^\d{6}$/.test(otp)) { Alert.alert('OTP required', 'Enter the six-digit OTP received by the customer.'); return; }
    perform(() => verifyOtp(appointment.id, otp), 'Customer OTP verified. The job is now in progress.');
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
  const canOtp = ['booked', 'confirmed'].includes(appointment.status) && state === 'arrived';
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
    {canOtp && <View style={styles.card}><Text style={styles.cardTitle}>Customer start OTP</Text><Text style={styles.help}>Ask the customer for the six-digit WhatsApp OTP. Never start without it.</Text>
      <Field label="OTP" value={otp} onChangeText={value => setOtp(value.replace(/\D/g, '').slice(0, 6))} keyboardType="numeric" /><Button title="Verify OTP & start job" onPress={verifyCustomerOtp} disabled={busy} /></View>}
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

export default function App() {
  const [session, setSession] = useState<MobileSession | null>(null);
  const [selected, setSelected] = useState<Appointment | null>(null);
  const [restoring, setRestoring] = useState(true);
  useEffect(() => { loadSession().then(setSession).finally(() => setRestoring(false)); }, []);
  if (restoring) return <SafeAreaView style={[styles.safe, styles.center]}><ActivityIndicator size="large" color={colors.green} /></SafeAreaView>;
  if (!session) return <LoginScreen onLogin={setSession} />;
  if (selected) return <JobScreen initial={selected} onBack={() => setSelected(null)} />;
  return <JobsScreen session={session} onOpen={setSelected} onSignedOut={() => setSession(null)} />;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.background }, center: { alignItems: 'center', justifyContent: 'center' },
  loginWrap: { flex: 1, justifyContent: 'center', padding: 24 },
  brandMark: { width: 58, height: 58, borderRadius: 18, backgroundColor: colors.green, alignItems: 'center', justifyContent: 'center', alignSelf: 'center' },
  brandMarkText: { color: '#fff', fontSize: 30, fontWeight: '800' }, title: { fontSize: 30, fontWeight: '800', color: colors.ink, textAlign: 'center', marginTop: 14 },
  subtitle: { fontSize: 15, color: colors.muted, textAlign: 'center', marginBottom: 26 }, privacy: { color: colors.muted, fontSize: 12, textAlign: 'center', lineHeight: 18, marginTop: 18 },
  header: { paddingHorizontal: 20, paddingVertical: 16, backgroundColor: colors.surface, borderBottomWidth: 1, borderBottomColor: colors.border, flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  eyebrow: { color: colors.green, fontSize: 11, fontWeight: '800', letterSpacing: 1.6 }, headerTitle: { color: colors.ink, fontSize: 21, fontWeight: '800' },
  signOut: { color: colors.danger, fontWeight: '700' }, back: { color: colors.green, fontWeight: '800', fontSize: 17 }, scroll: { padding: 18, paddingBottom: 48 },
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
  cardTitle: { color: colors.ink, fontSize: 18, fontWeight: '800', marginBottom: 6 }, help: { color: colors.muted, fontSize: 13, lineHeight: 19, marginBottom: 12 },
  empty: { padding: 30, borderWidth: 1, borderStyle: 'dashed', borderColor: colors.border, borderRadius: 16, alignItems: 'center' }, emptyText: { color: colors.muted },
});
