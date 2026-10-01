(() => {
  const DEFAULT_MESSAGE = 'Finish or cancel crop editing first.';
  const state = {
    active: false,
    message: DEFAULT_MESSAGE,
    source: null,
  };

  function computeEffectiveActive() {
    return Boolean(
      state.active ||
      document.body?.classList.contains('photo-crop-editor-open') ||
      document.documentElement.dataset.caseOverviewView === 'photo-crop'
    );
  }

  function snapshot() {
    return {
      active: computeEffectiveActive(),
      message: state.message,
      source: state.source,
    };
  }

  function emitChange() {
    document.documentElement.dataset.photoCropLock = computeEffectiveActive() ? 'active' : 'idle';
    document.dispatchEvent(
      new CustomEvent('dlyte:photo-crop-lock-changed', {
        detail: snapshot(),
      })
    );
  }

  function setActive(active, options = {}) {
    const nextActive = Boolean(active);
    const nextMessage = typeof options.message === 'string' && options.message.trim()
      ? options.message.trim()
      : DEFAULT_MESSAGE;
    const nextSource = options.source || null;
    const changed =
      state.active !== nextActive ||
      state.message !== nextMessage ||
      state.source !== nextSource;

    state.active = nextActive;
    state.message = nextMessage;
    state.source = nextSource;
    if (changed) emitChange();
  }

  function isActive() {
    return computeEffectiveActive();
  }

  function getMessage() {
    return state.message || DEFAULT_MESSAGE;
  }

  function guard(action = '') {
    if (!computeEffectiveActive()) return false;
    document.dispatchEvent(
      new CustomEvent('dlyte:photo-crop-lock-blocked', {
        detail: {
          action,
          message: getMessage(),
          source: state.source,
        },
      })
    );
    return true;
  }

  window.DLYTECropLock = {
    setActive,
    isActive,
    getMessage,
    guard,
    getState: snapshot,
  };

  emitChange();
})();
