import * as Location from 'expo-location';
import * as SecureStore from 'expo-secure-store';
import * as TaskManager from 'expo-task-manager';

import { ACTIVE_APPOINTMENT_KEY, sendLocation } from './api';

export const LOCATION_TASK_NAME = 'airzen-active-job-location';

TaskManager.defineTask(LOCATION_TASK_NAME, async ({ data, error }) => {
  if (error || !data) return;
  const appointmentId = await SecureStore.getItemAsync(ACTIVE_APPOINTMENT_KEY);
  if (!appointmentId) return;
  const locations = (data as { locations?: Location.LocationObject[] }).locations || [];
  for (const location of locations) {
    try {
      await sendLocation(appointmentId, location);
    } catch {
      // A later location update will retry naturally. The server endpoint is idempotent.
    }
  }
});

export async function currentCoordinates() {
  const foreground = await Location.requestForegroundPermissionsAsync();
  if (!foreground.granted) throw new Error('Location permission is required for field jobs.');
  const location = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High });
  return {
    latitude: location.coords.latitude,
    longitude: location.coords.longitude,
    accuracy_meters: location.coords.accuracy,
  };
}

export async function startJobTracking(appointmentId: string) {
  const foreground = await Location.requestForegroundPermissionsAsync();
  if (!foreground.granted) throw new Error('Foreground location permission is required.');
  const background = await Location.requestBackgroundPermissionsAsync();
  if (!background.granted) throw new Error('Allow background location so dispatch can track this active job.');
  await SecureStore.setItemAsync(ACTIVE_APPOINTMENT_KEY, appointmentId);
  if (await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK_NAME)) return;
  await Location.startLocationUpdatesAsync(LOCATION_TASK_NAME, {
    accuracy: Location.Accuracy.High,
    timeInterval: 15_000,
    distanceInterval: 20,
    deferredUpdatesDistance: 50,
    deferredUpdatesInterval: 30_000,
    pausesUpdatesAutomatically: false,
    foregroundService: {
      notificationTitle: 'AirZen job tracking active',
      notificationBody: 'Location sharing stops automatically when the assigned job is completed.',
      notificationColor: '#0B6E4F',
    },
  });
}

export async function stopJobTracking() {
  if (await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK_NAME)) {
    await Location.stopLocationUpdatesAsync(LOCATION_TASK_NAME);
  }
  await SecureStore.deleteItemAsync(ACTIVE_APPOINTMENT_KEY);
}
