# AirZen Staff Android App

AirZen Staff is the field-service companion for assigned technicians. It uses a
separate staff login and never stores CRM administrator credentials.

## Implemented workflow

1. Staff signs in with a unique CRM-managed username and a six-digit PIN.
2. The app lists only appointments assigned to that staff account.
3. **Start travel** requests foreground and background location permission and
   begins a visible Android foreground-service notification.
4. **Arrived** captures a front-camera selfie and a fresh GPS position.
5. The booking transaction creates a secret-backed six-digit service start
   code; the customer receives it with the booking confirmation.
6. After arrival, staff enters the customer's start code; a correct code moves
   the appointment to `in_progress`. Five incorrect attempts lock the code.
7. Staff records payment amount/method/reference, captures payment proof, and
   completes the job.
8. Completion atomically closes the appointment, updates the CRM lead, stores
   evidence and audit history, and queues the customer completion template.
9. Background tracking stops when the job completes or the staff signs out.

Selfies are photographic arrival evidence only. The app does not perform
biometric face recognition.

## Local setup

Requirements: Node.js 22.13 or newer, Android Studio, an Android device/emulator,
and an Expo development build. Background location does not work in Expo Go on
Android.

```bash
cp .env.example .env
npm install
npx expo prebuild --platform android
npm run android
```

For a physical phone, `EXPO_PUBLIC_AIRZEN_API_URL` must be an HTTPS URL reachable
from that phone.

## Backend rollout prerequisites

Do not deploy the mobile API until all of these are green:

- Apply `staff_mobile_schema.sql` to the intended MySQL database.
- Set a random `STAFF_MOBILE_TOKEN_SECRET` of at least 32 characters.
- Configure private S3-compatible evidence storage (`S3_ACCESS_KEY_ID`,
  `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, and optional endpoint/region).
- Configure the `service_completion_template`. A separate WhatsApp OTP
  template is not used to start a job.
- Provision each staff username and PIN from the CRM staff screen or through the authenticated admin endpoint
  `POST /api/appointment-staff/{staff_id}/mobile-access`.
- Verify `GET /api/staff-mobile/readiness` from an authenticated admin session.

## Privacy and operations

- Location is accepted only for an appointment assigned to the signed-in staff.
- Tracking is rejected after completion/cancellation/no-show/reschedule.
- Evidence is limited to 10 MB, encrypted at rest in private object storage,
  and exposed to CRM through five-minute signed links.
- Service start codes remain valid through the appointment plus a limited
  grace period. Only their hashes are stored in MySQL.
- The Android foreground-service notification remains visible while tracking.
- Production rollout still needs retention periods, staff privacy consent,
  Play Store privacy disclosures, and a documented lost-device process.
