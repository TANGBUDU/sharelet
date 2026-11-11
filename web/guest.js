"use strict";
(() => {
  const F = window.ShareletForms, slug = location.pathname.slice(3);
  const base = '/api/s/'+encodeURIComponent(slug), storage = 'sharelet:job:'+slug;
  const examples = {clay:'example_clay', anime:'example_anime', sticker:'example_sticker'};
  let share = null, pending = null, currentJob = null, lastJob = null, lastImage = null;
  let waiting = false, unknown = false, uploading=false, pollTimer = null, clockTimer = null;
  try { pending = JSON.parse(localStorage.getItem(storage) || 'null'); } catch {}
  function remember(value) {
    if (value) {
      try { localStorage.setItem(storage, JSON.stringify(value)); }
      catch { throw new Error(t('storage_required')); }
      pending = value;
    } else {
      pending = null;
      try { localStorage.removeItem(storage); } catch {}
    }
  }
  function isImage() { return share?.kind === 'image'; }
  function readable(value) {
    const box = document.createElement('div'); box.className = 'readable-result';
    const text = content => { const node = document.createElement('p'); node.className = 'result-text'; node.textContent = content; box.append(node); };
    const code = content => { const node = document.createElement('pre'); node.id = 'generic-result-text'; node.textContent = JSON.stringify(content, null, 2); box.append(node); };
    const scalar = item => item === null || ['string','number','boolean'].includes(typeof item);
    if (typeof value === 'string') { text(value); return box; }
    if (value && Array.isArray(value.content)) {
      // MCP tool results: text blocks as text, small images inline, nothing else is executed.
      for (const item of value.content) {
        if (item?.type === 'text' && typeof item.text === 'string') text(item.text);
        else if (item?.type === 'image' && /^image\/(png|jpeg|webp|gif)$/.test(item.mimeType || '') && typeof item.data === 'string' && /^[A-Za-z0-9+/=\s]+$/.test(item.data)) {
          const image = document.createElement('img'); image.className = 'result-image'; image.alt = t('result_ready');
          image.src = 'data:'+item.mimeType+';base64,'+item.data.replace(/\s/g,''); box.append(image);
        }
      }
      if (box.childElementCount) return box;
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const entries = Object.entries(value);
      if (entries.length && entries.length <= 16 && entries.every(([, item]) => scalar(item))) {
        const list = document.createElement('dl'); list.className = 'result-fields';
        for (const [key, item] of entries) {
          const term = document.createElement('dt'); term.textContent = I.content(key.replace(/_/g, ' '));
          const detail = document.createElement('dd'); detail.textContent = typeof item === 'boolean' ? t(item ? 'yes' : 'no') : item === null ? '—' : String(item);
          list.append(term, detail);
        }
        box.append(list); return box;
      }
    }
    code(value); return box;
  }
  function notify() { if(parent !== window) parent.postMessage({type:'sharelet:used'},location.origin); }
  function showError(error) { setError($('#guest-error'),error); }
  async function jsonResponse(response) {
    let value; try { value = await response.json(); } catch { throw new Error(t('connection_lost')); }
    if (!response.ok || !value.ok) { const error = new Error(value.error || t('loading_failed')); error.status = response.status; error.code = value.code; throw error; }
    return value;
  }
  async function get(path) { return jsonResponse(await fetch(path)); }
  function updateButton() {
    $('#guest-submit').disabled = waiting || uploading;
    $('#guest-submit-label').textContent = t(uploading ? 'file_uploading' : waiting ? 'running' : unknown ? 'check_status' : lastJob ? (isImage() ? 'generate_another' : 'run_again') : isImage() ? 'generate_image' : window.ShareletFiles?.isFileSchema(share?.schema) ? 'file_process' : 'run_tool');
    for (const field of $$('[data-form-field],#guest-subject')) field.required = !unknown && field.dataset.required === 'true';
  }
  function applyShare(value) {
    share = value; document.body.dataset.kind = share.kind;
    delete $('#guest-title').dataset.i18n;
    $('#guest-title').dataset.i18nContent=share.title;
    $('#guest-title').textContent = I.content(share.title); document.title = I.content(share.title)+' · Sharelet';
    delete $('#guest-description').dataset.i18nError;
    $('#guest-description').textContent = I.content(share.description); $('#guest-remaining').textContent = share.remaining;
    $('#guest-expiry').textContent = expiry(share.expires_at);
    $('#image-spec').textContent = isImage() ? (share.image_size || 1024)+' × '+(share.image_size || 1024) : t('tool_result');
    $('#example-row').hidden = !isImage();
    $('#canvas-empty h2').textContent = t(isImage() ? 'canvas_title' : 'tool_canvas_title');
    $('#canvas-empty p').textContent = t(isImage() ? 'canvas_description' : 'tool_canvas_description');
    $('#canvas-generating h2').textContent = t(isImage() ? 'making_magic' : 'running_tool');
    $('.guest-trust p').textContent = t(window.ShareletFiles?.isFileSchema(share.schema) ? 'file_privacy' : isImage() ? 'guest_privacy' : 'tool_privacy');
    const ended = ['expired','revoked','exhausted'].includes(share.status);
    $('#guest-closed').hidden = !ended || waiting; $('#guest-form').hidden = ended && !waiting;
    $('#closed-title').textContent = t(share.status === 'revoked' ? 'link_closed_title' : share.status === 'expired' ? 'link_expired_title' : 'link_spent_title');
    $('#closed-message').textContent = t(lastJob ? 'saved_result_note' : 'ask_creator');
    $('#download-image').querySelector('[data-i18n]').textContent = t(lastJob?.file_url ? 'file_download' : isImage() ? 'save_image' : 'save_result');
    updateButton();
  }
  function renderFields(previous = {}) {
    if(share?.preset==='weather')previous={...(window.ShareletWeather?.coordinates(pending?.weather_location)||{}),...previous};
    if (isImage()) {
      $('#guest-fields').innerHTML = '<label class="field-label" for="guest-subject" data-i18n="describe_character">'+t('describe_character')+'</label><textarea id="guest-subject" data-required="true" name="subject" minlength="3" maxlength="400" rows="4" required data-i18n-placeholder="subject_placeholder" placeholder="'+esc(t('subject_placeholder'))+'">'+esc(previous.subject || '')+'</textarea><p class="field-help" data-i18n="subject_help">'+t('subject_help')+'</p>';
    } else {
      F.guest(share.schema, $('#guest-fields'), previous);
      window.ShareletFiles?.bind($('#guest-fields'),base,{busy:value=>{uploading=value;updateButton();},error:showError,schema:share.schema});
      if(share.preset==='weather')window.ShareletWeather?.setup($('#guest-fields'));
    }
    updateButton();
  }
  function progress(on) {
    waiting = on; clearInterval(clockTimer);
    window.ShareletFiles?.running($('#guest-fields'),on);
    if (share) {
      const ended = ['expired','revoked','exhausted'].includes(share.status);
      $('#guest-closed').hidden = !ended || on;
      $('#guest-form').hidden = ended && !on;
    }
    $('#image-canvas').hidden = !on && !!lastJob && !isImage();
    $('#canvas-generating').hidden = !on; $('#canvas-empty').hidden = on || !!lastJob;
    $('#generated-image').hidden = on || !lastImage; $('#result-toolbar').hidden = on || !lastJob;
    $('#guest-result').hidden = on || !lastJob || isImage();
    if(on) {
      const tick = () => $('#generation-timer').textContent = t('elapsed', {count:Math.max(0,Math.floor(Date.now()/1000-(currentJob?.created_at || Date.now()/1000)))});
      tick(); clockTimer = setInterval(tick,1000);
    }
    updateButton();
  }
  function displayResult(job, scroll=true) {
    lastJob = job;
    if(isImage()) {
      lastImage = job.media_url; $('#generated-image').src = job.media_url;
      $('#generated-image').alt = t('generation_complete'); $('#download-image').href = job.media_url+'?download=1';
      $('#download-image').download = 'sharelet-image.png';
    } else {
      lastImage = null;
      if(!(share.preset==='weather' && window.ShareletWeather?.render(job.result,$('#guest-result'),pending?.weather_location)) &&
          !window.ShareletFiles?.result(job,$('#guest-result')) && !window.ShareletCanvasDemo?.render(job.result,$('#guest-result'))) {
        $('#guest-result').innerHTML = '<div class="generic-result-header">'+icon('check-circle')+'<strong>'+t('result_ready')+'</strong></div>';
        $('#guest-result').append(readable(job.result));
      }
      $('#download-image').href = (job.file_url || job.result_url)+'?download=1'; $('#download-image').download = job.file_url ? job.result.filename : 'sharelet-result.json';
      if(job.file_url)$('#download-image').querySelector('[data-i18n]').textContent=t('file_download');
    }
    progress(false); $('#closed-message').textContent = t('saved_result_note');
    if(scroll && innerWidth < 760) (isImage() ? $('#image-canvas') : $('#guest-result')).scrollIntoView({block:'center'});
  }
  async function refreshShare() { try { applyShare((await get(base)).share); } catch {} }
  async function watch(job) {
    currentJob = job; unknown = false;
    try { remember({...pending, job_id:job.id}); } catch(error) { showError(error); }
    if(job.state === 'succeeded') { clearTimeout(pollTimer); displayResult(job); window.ShareletFiles?.clear($('#guest-fields')); notify(); await refreshShare(); return; }
    if(['failed','interrupted'].includes(job.state)) { clearTimeout(pollTimer); progress(false); showError(job.error || t('job_failed')); window.ShareletFiles?.clear($('#guest-fields')); remember(null); notify(); await refreshShare(); return; }
    progress(true);
    pollTimer = setTimeout(async () => {
      try { const value = await get(base+'/jobs/'+job.id); applyShare(value.share); await watch(value.job); }
      catch(error) { unknown = true; progress(false); showError(error); }
    }, 1000);
  }
  async function recover() {
    if(!pending) return false;
    try {
      const value = await get(pending.job_id ? base+'/jobs/'+pending.job_id : base+'/requests/'+pending.request_id);
      if(value.share) applyShare(value.share); await watch(value.job); return true;
    } catch(error) {
      if(error.code === 'request_not_found') { remember(null); unknown = false; showError(t('request_not_started')); updateButton(); return false; }
      if(error.status === 410) { remember(null); progress(false); await refreshShare(); return true; }
      unknown = true; progress(false); showError(t('network_uncertain')); return true;
    }
  }
  async function load() {
    try { const value = await get(base); applyShare(value.share); renderFields(); if(pending && !['expired','revoked'].includes(share.status)) await recover(); }
    catch(error) { delete $('#guest-title').dataset.i18nContent; $('#guest-title').dataset.i18n='link_unavailable'; $('#guest-title').textContent = t('link_unavailable'); setError($('#guest-description'),error); $('#guest-form').hidden = true; }
  }
  $('#guest-form').onsubmit = async event => {
    event.preventDefault(); if(waiting || uploading || !share) return;
    if(unknown) { await recover(); return; }
    if(share.status !== 'active') return;
    $('#guest-error').hidden = true;
    let input;
    try {
      input = isImage() ? {subject:$('#guest-subject').value.trim()} : F.values(share.schema,$('#guest-fields'));
      if(!isImage())window.ShareletFiles?.validate(share.schema,input);
      if(isImage() && input.subject.length < 3) { $('#guest-subject').focus(); return; }
      remember({request_id:crypto.randomUUID().replaceAll('-',''),...(share.preset==='weather'?{weather_location:{lat:input.lat,lon:input.lon}}:{})});
    } catch(error) { showError(error); return; }
    const request_id = pending.request_id;
    lastJob = lastImage = null; currentJob = {created_at:Date.now()/1000}; $('#guest-result').textContent = ''; progress(true);
    try {
      const value = await jsonResponse(await fetch(base+'/jobs', {method:'POST',headers:{'Content-Type':'application/json'},
                                                                 body:JSON.stringify({input,request_id})}));
      applyShare(value.share); await watch(value.job); notify();
    } catch(error) { progress(false); const found = await recover(); if(!found) showError(error); await refreshShare(); }
  };
  $('#example-button').onclick = () => { const field = $('#guest-subject'); if(field) { field.value = t(examples[share?.preset] || examples.clay); field.focus(); } };
  $('#make-own').onclick = () => $('#own-dialog').showModal(); $('#own-close').onclick = () => $('#own-dialog').close();
  document.addEventListener('keydown', event => { if(event.key === 'Escape' && parent !== window) parent.postMessage({type:'sharelet:close'},location.origin); });
  document.addEventListener('sharelet:language', () => {
    if(!share) return;
    // Translate labels in place. Do not validate/rebuild inputs while switching
    // languages: incomplete drafts, enum values and running jobs must survive.
    applyShare(share); if(lastJob) displayResult(lastJob,false); window.ShareletFiles?.refresh($('#guest-fields')); updateButton();
  });
  load();
})();
