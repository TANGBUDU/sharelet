"use strict";
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const I=window.ShareletI18n, t=(key,args)=>I.t(key,args);
const shapes={
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  sparkles:'<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4zM20 2v4m-2-2h4"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 6-6 4 4 3-3 5 5"/>',
  sliders:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2" fill="currentColor"/><circle cx="16" cy="12" r="2" fill="currentColor"/><circle cx="10" cy="18" r="2" fill="currentColor"/>',
  book:'<path d="M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1v15"/>',
  'arrow-up-right':'<path d="M6 18 18 6M6 6h12v12"/>','arrow-right':'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',link:'<path d="m10 14 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 2 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0" transform="translate(1 0)"/>',
  'link-off':'<path d="m3 3 18 18M9 8a4 4 0 0 0-4 1l-2 2a4 4 0 0 0 6 6l2-2m3 1a4 4 0 0 0 5-1l2-2a4 4 0 0 0-6-6l-2 2"/>',
  play:'<path d="m8 4 12 8-12 8z"/>',check:'<path d="m5 12 4 4L19 6"/>','check-circle':'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',refresh:'<path d="M20 7v5h-5M4 17v-5h5M5.4 7a8 8 0 0 1 13-2L20 7M4 17l1.6 2a8 8 0 0 0 13-2"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',x:'<path d="m6 6 12 12M6 18 18 6"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',key:'<circle cx="8" cy="9" r="5"/><path d="m12 13 8 8m-4-4 3-3m-6 0 3-3"/>',
  download:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
};
const icon=name=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name]||shapes.sparkles}</svg>`;
function decorate(root=document){for(const el of $$('[data-icon]',root))if(!el.querySelector('svg'))el.innerHTML=icon(el.dataset.icon);}
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function expiry(ts){const seconds=Math.max(0,ts-Date.now()/1000);return seconds<=0?t('expired'):seconds<3600?t('expires_minutes',{count:Math.ceil(seconds/60)}):seconds<172800?t('expires_hours',{count:Math.ceil(seconds/3600)}):t('expires_days',{count:Math.ceil(seconds/86400)});}
function ago(ts){const s=Math.max(0,Date.now()/1000-ts);return s<60?t('just_now'):s<3600?t('minutes_ago',{count:Math.floor(s/60)}):s<86400?t('hours_ago',{count:Math.floor(s/3600)}):new Date(ts*1000).toLocaleDateString(I.locale,{month:'short',day:'numeric'});}
function presetTitle(tool){return tool.style?t('preset_'+tool.style+'_title'):I.content(tool.title);}
function presetDescription(tool){return tool.style?t('preset_'+tool.style+'_description'):I.content(tool.description);}
function setError(element,error){element.dataset.i18nError=JSON.stringify(typeof error==='string'?{message:error}:{message:error?.message||error?.error,code:error?.code,i18nKey:error?.i18nKey,i18nArgs:error?.i18nArgs});element.textContent=I.message(error);element.hidden=false;}
let toastTimer;
function toast(message){const el=$('#toast');if(!el)return;setError(el,message);clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,4000);}
async function copy(url){try{await navigator.clipboard.writeText(url);toast(t('copied'));}catch{$('#preview-link')?.select();toast(t('copy_manual'));}}
function recipeArt(style){
  const common='<ellipse cx="150" cy="142" rx="60" ry="9" fill="#343b2610"/>';
  if(style==='clay')return `<svg class="recipe-art" viewBox="0 0 300 175" aria-hidden="true">${common}<path d="m110 75-3-35 32 20m51 15 3-35-32 20" fill="#dba87f"/><path d="m117 64-3-18 17 13m52 5 3-18-17 13" fill="#f0c9a7"/><ellipse cx="150" cy="96" rx="49" ry="43" fill="#e1b48e"/><path d="M106 97c8-16 30-8 44 10 14-18 36-26 44-10-3 23-20 38-44 38s-41-15-44-38" fill="#f8ead8"/><circle cx="132" cy="90" r="4" fill="#534938"/><circle cx="168" cy="90" r="4" fill="#534938"/><path d="m145 105 5 5 5-5" fill="#71604a"/><path d="M128 132q22 12 44 0" stroke="#9eae89" stroke-width="12" stroke-linecap="round"/><path d="m169 134 7 14" stroke="#9eae89" stroke-width="10" stroke-linecap="round"/><path d="m215 44 3 7 7 3-7 3-3 7-3-7-7-3 7-3z" fill="#ccad8d"/><circle cx="83" cy="115" r="3" fill="#d0bda6"/></svg>`;
  if(style==='anime')return `<svg class="recipe-art" viewBox="0 0 300 175" aria-hidden="true">${common}<path d="M103 129V90q0-49 47-49t47 49v39" fill="#828093"/><path d="M113 88q0-35 37-35t37 35v18q-3 35-37 35t-37-35" fill="#f6e6da"/><path d="M109 89q-4-44 41-44 40 1 43 40-30 3-41-22-12 24-43 26" fill="#9d9cab"/><path d="M126 94h12m24 0h12" stroke="#5b5a6e" stroke-width="3" stroke-linecap="round"/><ellipse cx="133" cy="99" rx="3" ry="5" fill="#879c91"/><ellipse cx="168" cy="99" rx="3" ry="5" fill="#879c91"/><path d="M141 121q9 6 18 0" stroke="#c89588" stroke-width="2" fill="none"/><path d="m127 141 23-9 23 9" fill="#8a9e8a"/><path d="M99 127q-3-38 8-53m94 53q3-38-8-53" stroke="#aaa8b6" stroke-width="9" stroke-linecap="round"/><path d="m215 56 3 6 6 3-6 3-3 6-3-6-6-3 6-3z" fill="#b8b2cd"/></svg>`;
  return `<svg class="recipe-art" viewBox="0 0 300 175" aria-hidden="true">${common}<path d="m108 85-8-36 35 17q15-6 30 0l35-17-8 36q14 14 10 33-5 31-52 31t-52-31q-4-19 10-33" fill="#fff" stroke="#fff" stroke-width="13" stroke-linejoin="round"/><path d="m108 85-8-36 35 17q15-6 30 0l35-17-8 36q14 14 10 33-5 31-52 31t-52-31q-4-19 10-33" fill="#e9ba7c" stroke="#a77f51" stroke-width="2.5" stroke-linejoin="round"/><path d="m126 98 9 3m30 0 9-3" stroke="#785c3f" stroke-width="3" stroke-linecap="round"/><path d="m146 110 4 4 4-4" fill="#b18463"/><path d="M140 128q10 6 20 0" stroke="#a77f51" stroke-width="2" fill="none"/><rect x="167" y="119" width="36" height="27" rx="4" fill="#f8f4df" stroke="#8e9d80" stroke-width="2"/><path d="M203 125h5q9 8-5 14" fill="none" stroke="#8e9d80" stroke-width="3"/><path d="M179 111q-4-5 0-10m10 10q-4-5 0-10" stroke="#b4bfa8" stroke-width="2" fill="none" stroke-linecap="round"/></svg>`;
}
decorate();
