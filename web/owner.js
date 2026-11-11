"use strict";
(() => {
  const F = window.ShareletForms;
  let state = null, filter = 'active', toolFilter = 'all', selected = null, revokeId = null;
  let focusBefore = null, livePromise = null, refreshTimer = null, inspected = null, reviewDraft = null;
  function acceptEntry() {
    const key = new URLSearchParams(location.hash.slice(1)).get('owner');
    if (key) { sessionStorage.setItem('sharelet-owner', key); history.replaceState({}, '', location.pathname); }
  }
  acceptEntry();
  async function api(path, body) {
    const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST',
      headers:{Authorization:'Bearer '+(sessionStorage.getItem('sharelet-owner') || ''),
               ...(body === undefined ? {} : {'Content-Type':'application/json'})},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const value = await response.json();
    if (!response.ok || !value.ok) { const error = new Error(value.error || t('connection_lost')); error.status = response.status; error.code=value.code; throw error; }
    return value;
  }
  async function refresh(quiet = false) {
    try {
      state = await api('/api/owner/state'); render();
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => refresh(true), state.connections?.servers?.some(item => item.status === 'connecting') ? 1200 : 4000);
      return state;
    } catch (error) {
      if (error.status === 401) login();
      else if (!quiet) toast(error);
      return null;
    }
  }
  function login() {
    $('#connection-label').textContent = t('not_connected');
    $('#share-grid').innerHTML = '<div class="owner-login"><h2 data-i18n="creator_entry_title">'+t('creator_entry_title')+'</h2><p data-i18n="creator_entry_description">'+t('creator_entry_description')+
      '</p><form id="login-form"><input id="owner-input" type="password" autocomplete="off" required data-i18n-aria-label="private_creator_entry" aria-label="'+esc(t('private_creator_entry'))+'" data-i18n-placeholder="creator_entry_placeholder" placeholder="'+
      esc(t('creator_entry_placeholder'))+'"><button class="button primary" data-i18n="open_workspace">'+t('open_workspace')+'</button></form></div>';
    $('#login-form').onsubmit = async event => {
      event.preventDefault(); let key = $('#owner-input').value.trim();
      try { key = new URLSearchParams(new URL(key).hash.slice(1)).get('owner') || key; } catch {}
      sessionStorage.setItem('sharelet-owner', key); await refresh();
    };
  }
  function render() {
    if (!state) return;
    $('#connection-label').textContent = t(state.connection.mode === 'hermes' ? 'native' : 'portable');
    $('#stat-active').textContent = state.shares.filter(item => item.status === 'active').length;
    $('#stat-images').textContent = state.usage?.completed ?? state.activity.summary.success;
    $('#share-count').textContent = state.shares.length; $('#nav-count').textContent = state.shares.length;
    const images = state.tools.some(item => item.kind === 'image');
    if (!images) toolFilter = 'all';
    $('#tool-tabs').hidden = !images;
    $('#setup-banner').hidden = toolFilter !== 'images' || !!state.provider?.configured || !state.tools.some(item => item.kind === 'image');
    $('#share-caption').textContent = t(state.public_origin ? 'public_note' : 'local_note');
    const phase = state.public_link?.state || 'offline';
    // The guest address opens and reconnects by itself; this is status only.
    $('#live-button').classList.toggle('is-live', phase === 'live');
    $('#live-label').textContent = t(phase === 'live' ? 'live' : ['connecting','checking','installing'].includes(phase) ? 'connecting' : phase === 'failed' ? 'reconnecting' : 'public_idle');
    $('#public-error').hidden = phase !== 'failed';
    $('#public-error').textContent = phase === 'failed' ? t('public_reconnecting') : '';
    renderTools(); renderShares(); renderConnections(); renderDrafts();
    $('#activity-list').innerHTML = state.activity.runs.length ? state.activity.runs.map(run =>
      '<div class="activity-row"><span>'+icon(run.kind === 'image' ? 'sparkles' : 'sliders')+esc(I.content(run.title))+'</span><span class="run-status '+esc(run.state)+'">'+
      icon(run.state === 'success' ? 'check-circle' : run.state === 'failed' ? 'x' : 'clock')+t(run.state === 'success' ? 'success' : run.state)+
      '</span><span>'+(run.duration_ms == null ? '—' : t('seconds_duration',{count:(run.duration_ms/1000).toFixed(1)}))+'</span><span>'+ago(run.started_at)+'</span></div>').join('') :
      '<div class="table-empty">'+t('no_activity')+'</div>';
  }
  function renderTools() {
    const tools = state.tools.filter(item => toolFilter === 'all' || (toolFilter === 'images' ? item.kind === 'image' : item.kind !== 'image'));
    $('#tool-inventory-caption').textContent = t('tool_count', {count:tools.length});
    for (const button of $$('[data-tool-filter]')) { button.classList.toggle('active', button.dataset.toolFilter === toolFilter); button.setAttribute('aria-selected', String(button.dataset.toolFilter === toolFilter)); }
    $('#preset-grid').innerHTML = tools.length ? tools.map(tool => {
      const supported = !!tool.supported, image = tool.kind === 'image';
      const art = image ? '<div class="preset-art"><span class="preset-number">'+t('recipe_label')+'</span>'+recipeArt(tool.style)+'</div>' :
        '<div class="generic-tool-art">'+icon(tool.icon || 'sliders')+'<span>'+esc(tool.server==='sharelet_public' ? t('tool_source_builtin') : tool.source || tool.server)+'</span></div>';
      return '<button class="preset-card '+esc(tool.color || 'mint')+(image ? '' : ' generic-tool-card')+'" data-recipe="'+esc(tool.id)+'"'+(!supported ? ' disabled' : '')+'>'+
        art+'<div class="preset-copy"><h3>'+esc(presetTitle(tool))+'</h3><p>'+esc(supported ? presetDescription(tool) : I.message(tool.unsupported_reason))+'</p><span>'+
        t(!supported ? 'unsupported_tool' : image && !state.provider?.configured ? 'connect_to_use' : 'use_recipe')+(supported ? icon('arrow-up-right') : icon('lock'))+'</span></div></button>';
    }).join('') : '<div class="empty-state">'+icon('sliders')+'<h3>'+t('no_tools')+'</h3><p>'+t('no_tools_description')+'</p><button id="empty-connect" class="button secondary">'+t('connection')+'</button></div>';
    const connect = $('#empty-connect'); if (connect) connect.onclick = openSettings;
  }
  function renderShares() {
    const rows = state.shares.filter(item => filter === 'all' || (filter === 'active' ? item.status === 'active' : item.status !== 'active'));
    $('#share-grid').innerHTML = rows.length ? rows.map(share =>
      '<article class="share-card"><div class="share-card-top"><div class="tool-icon">'+icon(share.kind === 'image' ? 'sparkles' : 'sliders')+'</div><span class="status-tag'+
      (share.status === 'active' ? '' : ' inactive')+'"><i></i>'+t(share.status)+'</span></div><h3>'+esc(I.content(share.title))+'</h3><p class="card-description">'+
      esc(I.content(share.description))+'</p><div class="card-metrics"><span>'+icon('play')+t('runs_used',{used:share.calls,total:share.max_calls})+'</span><span>'+icon('clock')+
      (share.status === 'revoked' ? t('revoked') : expiry(share.expires_at))+'</span>'+(share.status === 'active' ? '<span>'+icon('link')+
      t(!share.public_enabled ? 'link_local' : share.online ? 'link_online' : 'link_waiting')+'</span>' : '')+'</div><progress class="share-progress" value="'+share.calls+'" max="'+share.max_calls+
      '" data-i18n-aria-label="run_allowance" aria-label="'+esc(t('run_allowance'))+'"></progress><div class="card-actions"><div class="card-actions-left"><button class="button secondary compact" data-action="copy" data-id="'+share.id+'">'+
      icon('copy')+t('copy_link')+'</button><button class="text-button" data-action="preview" data-id="'+share.id+'">'+icon('image')+t('preview')+'</button></div>'+
      (share.status !== 'revoked' ? '<button class="text-button revoke" data-action="revoke" data-id="'+share.id+'">'+t('close')+'</button>' : '')+'</div></article>').join('') :
      '<div class="empty-state">'+icon('link')+'<h3>'+t(state.shares.length ? filter === 'inactive' ? 'no_ended' : 'no_active' : 'no_links')+'</h3><p>'+t('no_links_description')+'</p></div>';
  }
  function renderConnections() {
    const connection = state.connections || {servers:[], import_supported:false};
    $('#connection-import-section').hidden = !connection.import_supported;
    $('#connection-list').innerHTML = '<p class="field-help">'+esc(t(connection.mode === 'hermes' ? 'native_connections_help' : 'portable_connections_help'))+'</p>'+
      (connection.servers.length ? connection.servers.map(server => '<div class="connection-row"><strong>'+esc(server.name)+'</strong><span class="status-tag'+
        (['ready','authorised'].includes(server.status) ? '' : ' inactive')+'">'+esc(t('connection_'+server.status))+'</span></div>').join('') :
        '<p class="field-help">'+t('no_external_connections')+'</p>');
  }
  function renderDrafts() {
    const rows = state.drafts || [];
    $('#approval-section').hidden = !rows.length;
    $('#approval-grid').innerHTML = rows.map(draft => '<article class="share-card"><span class="eyebrow">'+t('pending_approval')+'</span><h3>'+esc(I.content(draft.title))+
      '</h3><p>'+esc(I.content(draft.tool_title))+' · '+esc(draft.server)+'</p><p>'+t(draft.public ? 'request_public' : 'request_local')+' · '+esc(draft.max_calls)+' '+t('runs_left')+
      '</p><button class="button secondary compact" data-review="'+esc(draft.id)+'">'+t('review_request')+'</button></article>').join('');
  }
  function renderReview() {
    $('#draft-name').textContent = I.content(reviewDraft.title);
    const row = (label,value) => '<div class="review-row"><dt>'+esc(t(label))+'</dt><dd>'+value+'</dd></div>';
    const tool=state.tools.find(item=>item.server===reviewDraft.server && item.tool===reviewDraft.tool);
    const fieldName=name=>I.content(tool?.schema?.properties[name]?.title || name);
    const fieldValue=value=>typeof value==='boolean' ? t(value ? 'yes' : 'no') : value===null ? t('no_value') : I.content(typeof value==='string' ? value : JSON.stringify(value));
    const fixed = Object.entries(reviewDraft.fixed_args).map(([name,value]) => '<li><strong>'+esc(fieldName(name))+'</strong><code>'+esc(fieldValue(value))+'</code></li>').join('');
    $('#draft-settings').innerHTML = '<dl>'+row('review_tool',esc(I.content(reviewDraft.tool_title))+'<small>'+esc(I.content(reviewDraft.tool_description))+'</small>')+
      (reviewDraft.description ? row('guest_description',esc(I.content(reviewDraft.description))) : '')+
      row('review_fixed',fixed ? '<ul>'+fixed+'</ul>' : esc(t('review_none')))+
      row('review_guests',esc(reviewDraft.guest_fields.map(fieldName).join(', ') || t('review_none')))+
      row('review_allowance',esc(t('review_runs',{count:reviewDraft.max_calls})))+
      row('expires_after',esc(t('review_minutes',{count:reviewDraft.expires_seconds/60})))+
      row('review_audience',esc(t(reviewDraft.public ? 'request_public' : 'request_local')))+'</dl><details><summary>'+esc(t('review_identity'))+
      '</summary><code>'+esc(reviewDraft.server+' / '+reviewDraft.tool)+'</code></details>';
  }
  $('#approval-grid').onclick = event => {
    const button = event.target.closest('[data-review]'); if(!button) return;
    reviewDraft = state.drafts.find(item => item.id === button.dataset.review); if(!reviewDraft) return;
    renderReview();
    $('#draft-approved').checked = false; $('#draft-side-effects').checked = false;
    $('#draft-risk').hidden = !reviewDraft.requires_side_effects_approval;
    $('#draft-side-effects').required = !!reviewDraft.requires_side_effects_approval;
    $('#draft-error').hidden = true; $('#draft-dialog').showModal();
  };
  $('#draft-close').onclick = () => $('#draft-dialog').close();
  $('#draft-reject').onclick = async () => {
    if(!reviewDraft) return;
    try { await api('/api/owner/drafts/'+reviewDraft.id+'/reject',{}); $('#draft-dialog').close(); reviewDraft = null; await refresh(); }
    catch(error) { setError($('#draft-error'),error); }
  };
  $('#draft-form').onsubmit = async event => {
    event.preventDefault(); if(!reviewDraft) return; const draft = reviewDraft; $('#draft-approve').disabled = true;
    try {
      if(draft.kind === 'image' && !state.provider?.configured) throw new Error(t('image_needs_setup'));
      if(draft.public && !state.stable_links) await goLive();
      const result = await api('/api/owner/drafts/'+draft.id+'/approve',{review_digest:draft.review_digest,tool_review_hash:draft.tool_review_hash,
        public_safe_approved:$('#draft-approved').checked,allow_side_effects:$('#draft-side-effects').checked});
      $('#draft-dialog').close(); reviewDraft = null; await refresh();
      const share = state.shares.find(item => item.id === result.id); if(share) openPreview(share);
    } catch(error) { setError($('#draft-error'),error); }
    finally { $('#draft-approve').disabled = false; }
  };
  function closePanels() { $('#overlay').hidden = true; $$('.drawer').forEach(item => item.hidden = true); $('#guest-preview').src = 'about:blank'; document.body.style.overflow = ''; focusBefore?.focus(); }
  function openPanel(id) { focusBefore = document.activeElement; $('#overlay').hidden = false; $(id).hidden = false; document.body.style.overflow = 'hidden'; $('.close-panel', $(id)).focus(); }
  function openSettings() { $('#provider-error').hidden = true; $('#connections-error').hidden = true; $('#provider-key').value = ''; $('#provider-status').textContent = t(state?.provider?.configured ? 'key_saved' : 'key_missing');
    $('#use-host-key').hidden = !state?.provider?.host_key_available || !!state?.provider?.configured;
    $('#optional-images').hidden = !state?.tools?.some(item => item.kind === 'image');
    if(state) renderConnections(); $('#settings-dialog').showModal(); }
  function openCreate(id) {
    if (!state) return;
    const tool = state.tools.find(item => item.id === id) || state.tools.find(item => item.supported);
    if (!tool) { openSettings(); return; }
    if (tool.kind === 'image' && !state.provider?.configured) { openSettings(); $('.optional-images').open = true; toast(t('image_needs_setup')); return; }
    $('#create-form').reset(); $('#create-error').hidden = true; $('#more-settings').open = false;
    $('#tool-select').innerHTML = state.tools.filter(item => item.supported).map(item => '<option value="'+esc(item.id)+'">'+esc(presetTitle(item))+'</option>').join('');
    $('#tool-select').value = tool.id; updateRecipe(); openPanel('#create-drawer');
  }
  function updateRecipe() {
    selected = state.tools.find(item => item.id === $('#tool-select').value); if (!selected) return;
    $('#share-title').value = presetTitle(selected); $('#share-description').value = presetDescription(selected).slice(0,240);
    $('#tool-description').textContent = presetDescription(selected);
    const image = selected.kind === 'image'; $('#image-recipe-lock').hidden = !image; $('#image-notes').hidden = !image; $('#parameter-section').hidden = image;
    const risk = !!selected.requires_side_effects_approval;
    $('#tool-risk-box').hidden = !risk; $('#tool-risk-approval').required = risk; $('#tool-risk-approval').checked = false;
    if (!image) F.fixed(selected.schema, $('#parameter-editor'));
    summarise();
  }
  function summarise() {
    const days = Number($('#share-expiry').value) / 86400;
    $('#quick-summary').textContent = t('quick_summary', {runs:$('#share-limit').value || 0,
      period:days >= 1 ? t('days_count',{count:Math.round(days)}) : t('hours_count',{count:Math.round(days*24)})}) +
      ' · ' + t($('#local-only').checked ? 'link_local' : 'quick_public');
  }
  async function goLive() {
    if (state?.public_link?.state === 'live') return;
    if (livePromise) return livePromise;
    livePromise = (async () => {
      await api('/api/owner/public/start', {}); const until = Date.now()+145000;
      while (Date.now() < until) {
        await new Promise(resolve => setTimeout(resolve, 1000)); const next = await refresh(true);
        if (next?.public_link?.state === 'live') { toast(t('public_ready')); return; }
        if (next?.public_link?.state === 'failed') throw new Error(next.public_link.error || t('connection_lost'));
        if (next?.public_link?.state === 'offline') throw new Error(t('public_stopped'));
      }
      throw new Error(t('connection_lost'));
    })();
    try { return await livePromise; } finally { livePromise = null; }
  }
  async function copyShare(share) {
    if (share.public_enabled && !state.public_origin && !state.stable_links) { await goLive(); await refresh(); share = state.shares.find(item => item.id === share.id) || share; }
    await copy(share.url);
  }
  function openPreview(share) {
    $('#preview-link').value = share.url; $('#preview-open').href = share.url;
    $('#preview-copy').onclick = () => copyShare(share).catch(error => toast(error));
    $('#guest-preview').src = share.local_url || new URL(share.url).pathname; openPanel('#preview-drawer');
  }
  $('#preset-grid').onclick = event => { const button = event.target.closest('[data-recipe]'); if (button && !button.disabled) openCreate(button.dataset.recipe); };
  for (const button of $$('[data-tool-filter]')) button.onclick = () => { toolFilter = button.dataset.toolFilter; render(); };
  $('#refresh-tools').onclick = async () => { try { await api('/api/owner/tools/refresh', {}); await refresh(); } catch(error) { toast(error); } };
  $('#share-grid').onclick = event => {
    const button = event.target.closest('[data-action]'); if (!button || !state) return;
    const share = state.shares.find(item => item.id === button.dataset.id); if (!share) return;
    if (button.dataset.action === 'copy') copyShare(share).catch(error => toast(error));
    if (button.dataset.action === 'preview') openPreview(share);
    if (button.dataset.action === 'revoke') { revokeId = share.id; $('#revoke-dialog').showModal(); }
  };
  $('#create-form').onsubmit = async event => {
    event.preventDefault(); $('#publish-btn').disabled = true; $('#create-error').hidden = true;
    try {
      if (selected.kind === 'image' && !state.provider?.configured) throw new Error(t('image_needs_setup'));
      const fixed = selected.kind === 'image' ? {} : F.fixedValues(selected.schema, $('#parameter-editor'));
      const public_ = !$('#local-only').checked;
      if (public_ && !state.stable_links) { $('#publish-label').textContent = t('opening_secure'); await goLive(); }
      const created = await api('/api/owner/shares', {tool_id:selected.id, title:$('#share-title').value, description:$('#share-description').value,
        fixed_args:fixed, style_notes:selected.kind === 'image' ? $('#style-notes').value : '', max_calls:Number($('#share-limit').value),
        expires_seconds:Number($('#share-expiry').value), public_safe_approved:true, public:public_,
        allow_side_effects:$('#tool-risk-approval').checked,tool_review_hash:selected.review_hash});
      closePanels(); filter = 'active'; updateTabs(); await refresh(); const share = state.shares.find(item => item.id === created.id); if(share) openPreview(share); toast(t('created'));
    } catch(error) { setError($('#create-error'),error); }
    finally { $('#publish-btn').disabled = false; $('#publish-label').textContent = t('create_link'); }
  };
  $('#inspect-connections').onclick = async () => {
    $('#connections-error').hidden = true; $('#inspect-connections').disabled = true; inspected = null; $('#connect-selected').disabled = true;
    try {
      const path = $('#connection-path').value.trim(); const result = await api('/api/owner/connections/inspect', {path});
      inspected = path;
      $('#connection-choices').innerHTML = result.connections.servers.map(server => '<label class="server-choice"><input type="checkbox" value="'+esc(server.name)+'"'+
        (!server.supported ? ' disabled' : '')+'><span><strong>'+esc(server.name)+'</strong><small>'+esc(server.transport)+(server.program ? ' · '+esc(server.program) : '')+'</small></span></label>').join('') ||
        '<p class="field-help">'+t('no_servers_in_file')+'</p>';
      $('#connect-selected').disabled = false;
    } catch(error) { setError($('#connections-error'),error); }
    finally { $('#inspect-connections').disabled = false; }
  };
  $('#connection-path').oninput = () => { inspected = null; $('#connect-selected').disabled = true; };
  $('#connection-import-form').onsubmit = async event => {
    event.preventDefault(); if (!inspected) return; $('#connect-selected').disabled = true;
    try {
      const servers = $$('#connection-choices input:checked').map(item => item.value);
      await api('/api/owner/connections', {path:inspected, servers}); await refresh(); toast(t('connections_saved'));
    } catch(error) { setError($('#connections-error'),error); }
    finally { $('#connect-selected').disabled = false; }
  };
  $('#provider-form').onsubmit = async event => {
    event.preventDefault(); $('#provider-save').disabled = true; $('#provider-error').hidden = true;
    try { await api('/api/owner/provider', {openai_key:$('#provider-key').value}); $('#provider-key').value = ''; await refresh(); $('#provider-status').textContent = t('key_saved'); toast(t('provider_saved')); }
    catch(error) { setError($('#provider-error'),error); }
    finally { $('#provider-save').disabled = false; }
  };
  $('#use-host-key').onclick = async () => {
    try { await api('/api/owner/provider',{use_host_key:true}); await refresh(); $('#use-host-key').hidden=true; $('#provider-status').textContent=t('key_saved'); }
    catch(error) { setError($('#provider-error'),error); }
  };
  for (const id of ['#share-limit','#share-expiry','#local-only']) $(id).addEventListener('input', summarise);
  // Settings live in a collapsed section; open it if one of them needs attention.
  $('#create-form').addEventListener('invalid', event => { if ($('#more-settings').contains(event.target)) $('#more-settings').open = true; }, true);
  function updateTabs() { for(const tab of $$('.tab[data-filter]')) { tab.classList.toggle('active',tab.dataset.filter === filter); tab.setAttribute('aria-selected',String(tab.dataset.filter === filter)); } }
  for(const tab of $$('.tab[data-filter]')) tab.onclick = () => { filter = tab.dataset.filter; updateTabs(); if(state) renderShares(); };
  for(const button of $$('.close-panel')) button.onclick = closePanels;
  $('#overlay').onclick = closePanels; $('#create-top').onclick = () => openCreate(); $('#tool-select').onchange = updateRecipe; $('#refresh-btn').onclick = () => refresh();
  $('#nav-shares').onclick = () => $('#shares-section').scrollIntoView({block:'start'}); $('#nav-settings').onclick = openSettings; $('#setup-button').onclick = openSettings; $('#settings-close').onclick = () => $('#settings-dialog').close();
  $('#help-btn').onclick = () => $('#help-dialog').showModal(); $('#help-close').onclick = () => $('#help-dialog').close();
  $('#revoke-cancel').onclick = () => $('#revoke-dialog').close();
  $('#revoke-confirm').onclick = async () => { if(!revokeId) return; try { await api('/api/owner/shares/'+revokeId+'/revoke',{}); $('#revoke-dialog').close(); await refresh(); toast(t('closed_toast')); } catch(error) { toast(error); } finally { revokeId = null; } };
  document.addEventListener('keydown', event => {
    if($('#overlay').hidden) return;
    if(event.key === 'Escape') closePanels();
    if(event.key === 'Tab') {
      const panel = $$('.drawer').find(item => !item.hidden), controls = $$('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),iframe',panel).filter(item => item.getClientRects().length);
      if(event.shiftKey && document.activeElement === controls[0]) { controls.at(-1)?.focus(); event.preventDefault(); }
      else if(!event.shiftKey && document.activeElement === controls.at(-1)) { controls[0]?.focus(); event.preventDefault(); }
    }
  });
  window.addEventListener('hashchange', () => { acceptEntry(); refresh(); });
  window.addEventListener('message', event => { if(event.origin === location.origin && event.source === $('#guest-preview').contentWindow && event.data?.type === 'sharelet:used') refresh(true); });
  document.addEventListener('sharelet:language', () => {
    render(); updateTabs();
    if(reviewDraft && $('#draft-dialog').open) renderReview();
    if($('#settings-dialog').open) $('#provider-status').textContent=t(state?.provider?.configured ? 'key_saved' : 'key_missing');
    if(selected && !$('#create-drawer').hidden) summarise();
    if(selected) { $('#tool-description').textContent=presetDescription(selected); for(const option of $('#tool-select').options) { const tool=state.tools.find(item=>item.id===option.value); if(tool) option.textContent=presetTitle(tool); } }
  });
  refresh();
})();
