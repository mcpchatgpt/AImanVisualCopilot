importScripts('config.js');
const C = self.AVC_CONFIG;
let control = { enabled: false, devEnabled: false, checkedAt: 0, ok: false };
let lastSent = new Map();

const isDevUrl = (raw) => {
  try {
    const u = new URL(raw || '');
    const h = (u.hostname || '').toLowerCase();
    const p = Number(u.port || 0);
    return h === 'localhost' || h === '127.0.0.1' || h === '0.0.0.0' || h === '::1' ||
      h.startsWith('10.') || h.startsWith('192.168.') || /^172\.(1[6-9]|2\d|3[01])\./.test(h) ||
      h.endsWith('.local') || h.endsWith('.test') || [3000,3001,4000,4173,5000,5173,5174,8000,8080,8081,8888].includes(p);
  } catch (_) { return false; }
};

async function refreshControl(force=false) {
  const now = Date.now();
  if (!force && now - control.checkedAt < 2500) return control;
  try {
    const r = await fetch(C.server + '/api/v1/browser/control', {
      method: 'GET', cache: 'no-store', headers: {'Authorization': 'Bearer ' + C.token}
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    control = {enabled: !!d.monitoring_enabled, devEnabled: !!d.dev_observer_enabled, checkedAt: now, ok: true};
  } catch (_) {
    // Fail closed for privacy.
    control = {enabled: false, devEnabled: false, checkedAt: now, ok: false};
  }
  return control;
}

async function isLastFocusedActiveTab(tab) {
  if (!tab || !tab.active) return false;
  try {
    const w = await chrome.windows.getLastFocused({populate:false});
    return !!w && !!w.focused && w.id === tab.windowId;
  } catch (_) { return false; }
}

async function postDevEvent(payload, tab) {
  const ctl = await refreshControl(false);
  if (!ctl.ok || !ctl.enabled || !ctl.devEnabled || !tab || !(await isLastFocusedActiveTab(tab))) return {ok:false, skipped:'disabled_or_inactive'};
  const pageUrl = payload.page_url || tab.url || '';
  if (!isDevUrl(pageUrl)) return {ok:false, skipped:'not_dev_page'};
  const p = {...payload, page_url:pageUrl, tab_id:tab.id, window_id:tab.windowId, is_dev_page:true,
             extension_version:C.version, timestamp_unix:payload.timestamp_unix || Date.now()/1000};
  try {
    const r = await fetch(C.server + '/api/v1/browser/dev-event', {
      method:'POST', cache:'no-store', headers:{'Authorization':'Bearer '+C.token,'Content-Type':'application/json'}, body:JSON.stringify(p)
    });
    const d = await r.json().catch(()=>({}));
    if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
    return {ok:true,result:d};
  } catch(e) { return {ok:false,error:String(e)}; }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !sender.tab) return;
  if (msg.type === 'AVC_TICK') {
    (async () => {
      const ctl = await refreshControl(false);
      const active = await isLastFocusedActiveTab(sender.tab);
      sendResponse({capture: ctl.ok && ctl.enabled && active, dev_capture: ctl.ok && ctl.enabled && ctl.devEnabled && active && isDevUrl(sender.tab.url)});
    })();
    return true;
  }
  if (msg.type === 'AVC_SNAPSHOT') {
    (async () => {
      const ctl = await refreshControl(false);
      if (!ctl.ok || !ctl.enabled || !(await isLastFocusedActiveTab(sender.tab))) {
        sendResponse({ok:false, skipped:'disabled_or_inactive'}); return;
      }
      const p = msg.payload || {};
      p.tab_id = sender.tab.id; p.window_id = sender.tab.windowId; p.active = true;
      p.extension_version = C.version; p.timestamp_unix = Date.now()/1000;
      const inputKey = (p.input_events || []).map(x=>[x.kind,x.key,x.timestamp_unix]).flat().join(':');
      const key = sender.tab.id + ':' + (p.semantic_hash || '') + ':' + JSON.stringify(p.viewport || {}) + ':' + inputKey;
      const prev = lastSent.get(sender.tab.id); const now = Date.now();
      if (prev && prev.key === key && now - prev.at < 8000) { sendResponse({ok:true, skipped:'dedup'}); return; }
      try {
        const r = await fetch(C.server + '/api/v1/browser/snapshot', {
          method:'POST', cache:'no-store', headers:{'Authorization':'Bearer '+C.token,'Content-Type':'application/json'}, body: JSON.stringify(p)
        });
        const d = await r.json().catch(()=>({}));
        if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
        lastSent.set(sender.tab.id,{key,at:now}); sendResponse({ok:true, result:d});
      } catch(e) { sendResponse({ok:false,error:String(e)}); }
    })();
    return true;
  }
  if (msg.type === 'AVC_DEV_EVENT') {
    (async()=>sendResponse(await postDevEvent(msg.payload || {}, sender.tab)))();
    return true;
  }
});

async function devTabFor(details) {
  if (details.tabId == null || details.tabId < 0) return null;
  try { return await chrome.tabs.get(details.tabId); } catch (_) { return null; }
}

chrome.webRequest.onCompleted.addListener((details) => {
  if (!details || details.statusCode < 400) return;
  (async()=>{
    const tab = await devTabFor(details); if (!tab || !isDevUrl(tab.url)) return;
    const main = details.type === 'main_frame';
    const severity = details.statusCode >= 500 ? 'high' : main ? 'medium' : 'low';
    await postDevEvent({event_type:'http_error', severity,
      summary:`HTTP ${details.statusCode} ${details.method || ''} ${details.url}`.slice(0,1000),
      detail:{status_code:details.statusCode,method:details.method||'',resource_url:details.url,type:details.type,from_cache:!!details.fromCache}}, tab);
  })();
}, {urls:['http://*/*','https://*/*']});

chrome.webRequest.onErrorOccurred.addListener((details) => {
  (async()=>{
    const tab = await devTabFor(details); if (!tab || !isDevUrl(tab.url)) return;
    await postDevEvent({event_type:'network_error', severity:'high',
      summary:`Network error: ${details.error || 'request failed'}`,
      detail:{error:details.error||'',method:details.method||'',resource_url:details.url,type:details.type}}, tab);
  })();
}, {urls:['http://*/*','https://*/*']});

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) return;
  (async()=>{
    const tab = await devTabFor(details); if (!tab || !isDevUrl(tab.url)) return;
    await postDevEvent({event_type:'navigation', severity:'info', summary:`Navigation: ${tab.url}`,
      detail:{transition_type:details.transitionType||'',transition_qualifiers:details.transitionQualifiers||[]}}, tab);
  })();
});

chrome.tabs.onActivated.addListener(()=>refreshControl(true));
chrome.windows.onFocusChanged.addListener(()=>refreshControl(true));
