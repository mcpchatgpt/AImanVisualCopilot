(() => {
  if (window.__avcBridgeLoaded) return;
  window.__avcBridgeLoaded = true;
  let revision = 1, lastBuiltRevision = 0, lastBuiltAt = 0, tracking = false, observer = null;
  let lastBump = 0;
  const bump = () => { const n=Date.now(); if(n-lastBump>180){revision++;lastBump=n;} };
  const events = [['scroll',bump,{passive:true}],['resize',bump,{passive:true}],['focusin',bump,true],['popstate',bump,true],['hashchange',bump,true]];
  function setTracking(on){
    if(on===tracking) return;
    tracking=on;
    if(on){
      try { observer=new MutationObserver(bump); observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['aria-label','aria-selected','aria-expanded','disabled','href','class']}); } catch(_) {}
      for(const [n,f,o] of events) addEventListener(n,f,o);
      revision++;
    } else {
      try { observer?.disconnect(); } catch(_) {} observer=null;
      for(const [n,f,o] of events) try{removeEventListener(n,f,o)}catch(_){}
      lastBuiltRevision=0; lastBuiltAt=0;
    }
  }
  const clean=(v,n=500)=>String(v??'').replace(/\s+/g,' ').trim().slice(0,n);
  const visible=el=>{try{const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'}catch(_){return false}};
  const sensitive=el=>{const type=(el.getAttribute?.('type')||'').toLowerCase(),ac=(el.getAttribute?.('autocomplete')||'').toLowerCase();return type==='password'||/password|one-time-code|cc-number|cc-csc|cc-exp/.test(ac)};
  function cssSelector(el){
    try{
      if(el.id) return '#'+CSS.escape(el.id);
      const al=el.getAttribute?.('aria-label'); if(al) return (el.tagName||'').toLowerCase()+'[aria-label="'+CSS.escape(al)+'"]';
      const parts=[]; let cur=el;
      for(let depth=0;cur&&cur.nodeType===1&&depth<4;depth++,cur=cur.parentElement){
        let part=(cur.tagName||'').toLowerCase(); if(!part) break;
        const siblings=cur.parentElement?[...cur.parentElement.children].filter(x=>x.tagName===cur.tagName):[];
        if(siblings.length>1) part += ':nth-of-type('+(siblings.indexOf(cur)+1)+')';
        parts.unshift(part);
      }
      return parts.join(' > ').slice(0,500);
    }catch(_){return ''}
  }
  function elementInfo(el){
    const tag=(el.tagName||'').toLowerCase(),role=clean(el.getAttribute?.('role')||tag,80);
    const name=clean(el.getAttribute?.('aria-label')||el.getAttribute?.('title')||el.innerText||el.textContent||el.getAttribute?.('placeholder')||'',300);
    let value=''; if('value' in el&&!sensitive(el)) value=clean(el.value,300); else if(sensitive(el)) value='[REDACTED]';
    const r=el.getBoundingClientRect?.(); return {tag,role,name,value,href:tag==='a'?clean(el.href,1000):'',id:clean(el.id,120),selector:cssSelector(el),disabled:!!el.disabled,
      checked:'checked' in el?!!el.checked:undefined,selected:el.getAttribute?.('aria-selected')||'',expanded:el.getAttribute?.('aria-expanded')||'',
      x:r?Math.round(r.x):0,y:r?Math.round(r.y):0,width:r?Math.round(r.width):0,height:r?Math.round(r.height):0};
  }
  function hash32(s){let h=2166136261>>>0;for(let i=0;i<s.length;i++){h^=s.charCodeAt(i);h=Math.imul(h,16777619)}return('00000000'+(h>>>0).toString(16)).slice(-8)}
  function build(){
    const bodyText=clean(document.body?.innerText||'',30000);
    const headings=[...document.querySelectorAll('h1,h2,h3,[role="heading"]')].filter(visible).slice(0,80).map(e=>({level:e.tagName?.match(/^H([1-6])$/)?.[1]||e.getAttribute('aria-level')||'',text:clean(e.innerText||e.textContent,500)}));
    const controls=[...document.querySelectorAll('a,button,input,select,textarea,[role="button"],[role="link"],[role="tab"],[role="checkbox"],[role="radio"],[role="menuitem"],[contenteditable="true"]')].filter(visible).slice(0,180).map(elementInfo);
    const landmarks=[...document.querySelectorAll('main,nav,header,footer,aside,[role="main"],[role="navigation"],[role="dialog"],[role="alert"]')].filter(visible).slice(0,60).map(e=>({tag:(e.tagName||'').toLowerCase(),role:clean(e.getAttribute('role')||'',80),name:clean(e.getAttribute('aria-label')||e.getAttribute('title')||'',300)}));
    const ae=document.activeElement,focus=ae&&ae!==document.body?elementInfo(ae):{};
    const viewport={x:Math.round(scrollX),y:Math.round(scrollY),width:innerWidth,height:innerHeight,document_width:document.documentElement?.scrollWidth||0,document_height:document.documentElement?.scrollHeight||0,device_pixel_ratio:devicePixelRatio||1};
    const dom={headings,controls,landmarks,language:document.documentElement?.lang||'',content_type:document.contentType||''};
    const sig=[location.href,document.title,bodyText.slice(0,12000),JSON.stringify(headings),JSON.stringify(controls.slice(0,80)),JSON.stringify(viewport)].join('\n');
    return {url:location.href,title:document.title,visible_text:bodyText,dom,focus,viewport,revision,semantic_hash:hash32(sig),page_visibility:document.visibilityState};
  }
  const isDevLocation=()=>{try{const h=location.hostname.toLowerCase(),p=Number(location.port||0);return h==='localhost'||h==='127.0.0.1'||h==='0.0.0.0'||h==='::1'||h.startsWith('10.')||h.startsWith('192.168.')||/^172\.(1[6-9]|2\d|3[01])\./.test(h)||h.endsWith('.local')||h.endsWith('.test')||[3000,3001,4000,4173,5000,5173,5174,8000,8080,8081,8888].includes(p)}catch(_){return false}};
  let devPerfSent=false;
  async function sendDev(event_type,severity,summary,detail={}){
    if(!tracking||!isDevLocation())return;
    try{await chrome.runtime.sendMessage({type:'AVC_DEV_EVENT',payload:{event_type,severity,summary,detail,page_url:location.href}})}catch(_){}
  }
  async function maybeSendPerf(){
    if(devPerfSent||!tracking||!isDevLocation())return;
    try{
      const nav=performance.getEntriesByType('navigation')?.[0]; if(!nav)return;
      const duration=Math.round(nav.duration||0), dom=Math.round(nav.domContentLoadedEventEnd||0), load=Math.round(nav.loadEventEnd||0);
      devPerfSent=true;
      await sendDev('page_performance',duration>10000?'medium':duration>5000?'low':'info',`Page load ${duration} ms`,{
        duration_ms:duration,dom_content_loaded_ms:dom,load_event_ms:load,transfer_size:nav.transferSize||0,encoded_body_size:nav.encodedBodySize||0,decoded_body_size:nav.decodedBodySize||0,type:nav.type||''
      });
    }catch(_){}
  }
  async function tick(){
    try{
      const permission=await chrome.runtime.sendMessage({type:'AVC_TICK'});
      if(!permission?.capture){setTracking(false);return}
      setTracking(true);
      if(permission?.dev_capture) maybeSendPerf();
      const now=Date.now(); if(revision===lastBuiltRevision&&now-lastBuiltAt<8000)return;
      const payload=build(); lastBuiltRevision=revision; lastBuiltAt=now;
      await chrome.runtime.sendMessage({type:'AVC_SNAPSHOT',payload});
    }catch(_){setTracking(false)}
  }
  setInterval(tick,1800); setTimeout(tick,350);
})();
