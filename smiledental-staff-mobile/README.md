# Smile Dental Staff Mobile App (v2.0)

Dedicated Android & iOS companion application for doctors, dental nurses, and clinic receptionists across Smile Dental clinic branches (`ECIL / Kapra`, `AS Rao Nagar`, `Sainikpuri`).

---

## ✨ Features
- **Clinic Branch Switcher**: Instant switching between `ECIL / Kapra (Main Clinic)`, `AS Rao Nagar Branch`, and `Sainikpuri Branch`.
- **Today's Patient Queue**: Real-time consultation queue with dental chair assignment (`Chair 1`, `Chair 2`, `Consultation`) and procedure tracking.
- **Doctor KPIs Dashboard**: Live counts for **Today's Bookings**, **This Week**, **This Month**, and doctor/branch breakdown.
- **Duty Shift Attendance**: 1-tap Check-In & Check-Out with shift remarks.
- **Staff Leave Requests**: Submit medical, casual, annual, or emergency leave requests with status tracking.
- **Multi-Branch Revenue & Presence (Admin)**: Multi-branch revenue overview and staff presence status.

---

## 🚀 Running the App

### Prerequisites
- Node.js 18+
- Expo CLI

### Setup & Launch
```bash
# Navigate to staff mobile folder
cd d:\Vloicecallcrm-main\smiledental-voicecallcrm\smiledental-staff-mobile

# Start Expo development server
npm run start

# Run on Android emulator / connected device
npm run android
```

### Backend Connection
The app connects to the Smile Dental CRM backend on port 8000:
- Development default: `http://127.0.0.1:8000` (or `http://10.0.2.2:8000` on Android Emulator)
- Configure via `.env`:
```env
EXPO_PUBLIC_SMILEDENTAL_API_URL=http://127.0.0.1:8000
```
