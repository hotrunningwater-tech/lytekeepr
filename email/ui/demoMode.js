const DEMO_MODE_KEY = 'dlyte-demo-mode';

export function isDemoModeEnabled() {
  try {
    return window.localStorage.getItem(DEMO_MODE_KEY) !== 'off';
  } catch (err) {
    return true;
  }
}

export function setDemoModeEnabled(enabled) {
  try {
    window.localStorage.setItem(DEMO_MODE_KEY, enabled ? 'on' : 'off');
  } catch (err) {
    // Ignore localStorage failures; default masking still applies for this render.
  }
}

export function toggleDemoMode() {
  const nextValue = !isDemoModeEnabled();
  setDemoModeEnabled(nextValue);
  return nextValue;
}

export function getMaskedAccountLabel(accountId, index) {
  const suffix = index + 1;
  return `•••••••• ${suffix}`;
}
