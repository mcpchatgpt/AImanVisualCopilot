(() => {
  if (window.__avcDevMainLoaded) return;
  window.__avcDevMainLoaded = true;
  let enabled = false;
  let handlersAttached = false;

  const isDev = () => {
    try {
      const h = location.hostname.toLowerCase();
      const p = Number(location.port || 0);
      return h === 'localhost' || h === '127.0.0.1' || h === '0.0.0.0' || h === '::1' ||
        h.startsWith('10.') || h.startsWith('192.168.') || /^172\.(1[6-9]|2\d|3[01])\./.test(h) ||
        h.endsWith('.local') || h.endsWith('.test') || [3000,3001,4000,4173,5000,5173,5174,8000,8080,8081,8888].includes(p);
    } catch (_) { return false; }
  };
  if (!isDev()) return;
  const clean = (v,n=4000) => String(v ?? '').slice(0,n);
  const emit = (event_type, severity, summary, detail={}) => {
    if (!enabled) return;
    try { window.postMessage({source:'AVC_DEV_MAIN', event_type, severity, summary:clean(summary,1000), detail, page_url:location.href}, '*'); } catch (_) {}
  };
  const onError = (e) => {
    const target = e.target;
    if (target && target !== window && (target.src || target.href)) {
      emit('resource_error','medium','Resource failed to load',{resource:clean(target.src || target.href,2000),tag:clean(target.tagName,40)});
      return;
    }
    const err = e.error;
    emit('js_error','high', e.message || 'JavaScript error', {
      message:clean(e.message,2000), source:clean(e.filename,2000), line:e.lineno||0, column:e.colno||0,
      stack:clean(err && err.stack,4000)
    });
  };
  const onRejection = (e) => {
    const reason = e.reason;
    const message = reason && (reason.message || reason.toString?.()) || 'Unhandled promise rejection';
    emit('unhandled_rejection','high', message, {message:clean(message,2000),stack:clean(reason && reason.stack,4000)});
  };
  function setEnabled(on) {
    enabled = !!on;
    if (enabled && !handlersAttached) {
      addEventListener('error', onError, true);
      addEventListener('unhandledrejection', onRejection);
      handlersAttached = true;
    } else if (!enabled && handlersAttached) {
      removeEventListener('error', onError, true);
      removeEventListener('unhandledrejection', onRejection);
      handlersAttached = false;
    }
  }
  addEventListener('message', (e) => {
    const d=e.data;
    if (e.source===window && d && d.source==='AVC_DEV_BRIDGE' && d.type==='CONTROL') setEnabled(!!d.enabled);
  });
})();
