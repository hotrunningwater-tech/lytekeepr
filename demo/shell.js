const DEFAULT_SHELL = {
  shellVariant: 'default',
  brandMode: 'text-logo',
  productName: 'DLYTE',
  identityLabel: 'Your case',
  identityValue: 'Private workspace',
  vars: {},
};

const SHELL_CONFIG = {
  default: {
    shellVariant: 'default',
    brandMode: 'text-logo',
    productName: 'DLYTE',
    identityLabel: 'Your case',
    identityValue: 'Private workspace',
    vars: {},
  },
  variant_1: {
    shellVariant: 'variant_1',
    brandMode: 'circle-only',
    productName: 'HighLyter',
    identityLabel: 'Product shell',
    identityValue: 'HighLyter workspace',
    vars: {
      '--header-surface': '#F1FF00',
      '--hero-surface': '#F6F0E7',
      '--brand-surface': '#F1FF00',
      '--header-overlay': 'rgba(255, 255, 255, 0.22)',
      '--hero-overlay': 'rgba(255, 255, 255, 0.08)',
      '--surface': '#FFFFFF',
      '--ink': '#211B1A',
      '--ink-soft': '#786F89',
      '--coral': '#786F89',
      '--teal': '#786F89',
      '--teal-soft': '#DDD7E5',
      '--deep': '#211B1A',
      '--backdrop': '#F6F0E7',
      '--line': '#E6DDD1',
    },
  },
  variant_2: {
    shellVariant: 'variant_2',
    brandMode: 'circle-only',
    productName: 'Light Haus',
    identityLabel: 'Product shell',
    identityValue: 'Light Haus workspace',
    vars: {
      '--header-surface': '#B88A6A',
      '--hero-surface': '#F6EEE7',
      '--brand-surface': '#B88A6A',
      '--header-overlay': 'rgba(255, 255, 255, 0.22)',
      '--hero-overlay': 'rgba(255, 255, 255, 0.1)',
      '--surface': '#FFFFFF',
      '--ink': '#2E241F',
      '--ink-soft': '#6E7F86',
      '--coral': '#B88A6A',
      '--teal': '#6E7F86',
      '--teal-soft': '#D9E1E3',
      '--deep': '#2E241F',
      '--backdrop': '#F6EEE7',
      '--line': '#E7D8CC',
    },
  },
  variant_3: {
    shellVariant: 'variant_3',
    brandMode: 'circle-only',
    productName: 'LyteKeepr (obsolete)',
    identityLabel: 'Product shell',
    identityValue: 'LyteKeepr (obsolete) workspace',
    vars: {
      '--header-surface': '#9fc38b',
      '--hero-surface': '#b5d2a5',
      '--brand-surface': '#6f9c53',
      '--ink': '#24422d',
      '--ink-soft': '#4f6d57',
      '--coral': '#5d8a46',
      '--teal': '#5f8969',
      '--teal-soft': '#9dc0a0',
      '--deep': '#2c5a37',
      '--backdrop': '#b2c8b1',
    },
  },
};

function applyShell(config) {
  const root = document.documentElement;
  const nextConfig = config || DEFAULT_SHELL;
  const shellIdentityLabelEl = document.getElementById('shellIdentityLabel');
  const shellIdentityValueEl = document.getElementById('shellIdentityValue');
  const brandNameEl = document.querySelector('.brand-name');
  const productName = nextConfig.productName || 'DLYTE';
  root.dataset.shell = nextConfig.shellVariant || 'default';
  root.dataset.shellBrand = nextConfig.brandMode || 'text-logo';
  root.dataset.shellProductName = productName;

  if (brandNameEl) {
    brandNameEl.textContent = productName;
  }
  if (shellIdentityLabelEl) {
    shellIdentityLabelEl.textContent = nextConfig.identityLabel || 'Your case';
  }
  if (shellIdentityValueEl) {
    shellIdentityValueEl.textContent = nextConfig.identityValue || 'Private workspace';
  }

  const knownVars = new Set();
  Object.values(SHELL_CONFIG).forEach((entry) => {
    Object.keys(entry.vars || {}).forEach((key) => knownVars.add(key));
  });

  for (const key of knownVars) {
    root.style.removeProperty(key);
  }
  for (const [key, value] of Object.entries(nextConfig.vars || {})) {
    root.style.setProperty(key, value);
  }

  document.dispatchEvent(new CustomEvent('dlyte:shell-changed', {
    detail: {
      shellVariant: root.dataset.shell,
      productName,
    },
  }));
}

async function loadShell() {
  applyShell(DEFAULT_SHELL);
  try {
    const res = await fetch('/api/gmail/status');
    const data = await res.json();
    const shellVariant = data?.active_shell_variant || 'default';
    applyShell(SHELL_CONFIG[shellVariant] || DEFAULT_SHELL);
  } catch (err) {
    applyShell(DEFAULT_SHELL);
  }
}

loadShell();
