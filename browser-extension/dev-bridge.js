(() => {
  if (window.__avcDevBridgeLoaded) return;
  window.__avcDevBridgeLoaded = true;
  let devCapture = false;

  async function syncControl() {
    try {
      const p = await chrome.runtime.sendMessage({type:'AVC_TICK'});
      devCapture = !!p?.dev_capture;
    } catch (_) { devCapture = false; }
    try { window.postMessage({source:'AVC_DEV_BRIDGE',type:'CONTROL',enabled:devCapture}, '*'); } catch (_) {}
  }

  addEventListener('message', (e) => {
    const d = e.data;
    if (e.source !== window || !d || d.source !== 'AVC_DEV_MAIN' || !devCapture) return;
    chrome.runtime.sendMessage({type:'AVC_DEV_EVENT',payload:{
      event_type:d.event_type||'dev_event', severity:d.severity||'info', summary:d.summary||'Developer event',
      detail:d.detail||{}, page_url:d.page_url||location.href
    }}).catch(()=>{});
  });

  syncControl();
  setInterval(syncControl, 2500);
})();
