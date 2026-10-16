// Run in the head so a saved override applies before the page is painted.
(() => {
  const key = 'api-agent-studio-appearance';
  const stylesheet = document.getElementById('dark-theme');
  const systemScheme = matchMedia('(prefers-color-scheme: dark)');
  function updateSystemMode() {
    const mode = document.getElementById('system-mode');
    if (mode) mode.textContent = systemScheme.matches ? 'dark' : 'light';
  }
  const normalize = value => ['light', 'dark'].includes(value) ? value : 'system';
  let preference = 'system', revision = 0;
  try { preference = normalize(localStorage.getItem(key)); } catch {}
  function apply(value) {
    const current = ++revision;
    document.documentElement.setAttribute('data-appearance-changing', '');
    preference = normalize(value);
    stylesheet.media = preference === 'system' ? '(prefers-color-scheme: dark)' : preference === 'dark' ? 'all' : 'not all';
    const select = document.getElementById('appearance');
    if (select) select.value = preference;
    // Apply both text and surface colors together, without mismatched transition frames.
    void getComputedStyle(document.documentElement).color;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (revision === current) document.documentElement.removeAttribute('data-appearance-changing');
    }));
  }
  apply(preference);
  systemScheme.addEventListener('change', () => {
    updateSystemMode();
    if (preference === 'system') apply('system');
  });
  document.addEventListener('DOMContentLoaded', () => {
    updateSystemMode();
    const select = document.getElementById('appearance');
    select.value = preference;
    select.addEventListener('change', () => {
      apply(select.value);
      const status = document.getElementById('appearance-status');
      try {
        if (preference === 'system') localStorage.removeItem(key);
        else localStorage.setItem(key, preference);
        status.hidden = true; status.textContent = '';
      } catch {
        status.textContent = 'Appearance changed for this page. Your browser could not save the preference.';
        status.hidden = false;
      }
    });
  });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();
