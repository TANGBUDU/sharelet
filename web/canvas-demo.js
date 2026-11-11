"use strict";
// A bounded, passive view of the optional Permission Canvas sandbox receipt.
// Tool content is always text or a small PNG; it cannot add scripts or controls.
window.ShareletCanvasDemo = (() => {
  const decisions=new Set(['allowed','blocked','approval_required','observed']);
  const levels=new Set(['green','blue','yellow','black']);
  function preview(value) {
    if(typeof value!=='string' || value.length<48 || value.length>24000 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value)) return null;
    try {
      const bytes=atob(value), signature=[137,80,78,71,13,10,26,10];
      if(signature.some((byte,index)=>bytes.charCodeAt(index)!==byte) || bytes.slice(12,16)!=='IHDR') return null;
      const number=offset=>[0,1,2,3].reduce((result,index)=>result*256+bytes.charCodeAt(offset+index),0);
      if(number(16)!==800 || number(20)!==480) return null;
      return 'data:image/png;base64,'+value;
    } catch {return null;}
  }
  function accepts(value) {
    return !!value && value.kind==='permission_canvas_demo' && value.schema_version===1 && value.sandbox===true &&
      decisions.has(value.decision) && levels.has(value.level) && [0,1].includes(value.execution_count) &&
      value.hidden_controls===1 && value.pixels_masked===true && value.credential_exposed===false &&
      typeof value.plugin_version==='string' && /^\d+\.\d+\.\d+$/.test(value.plugin_version) &&
      typeof value.source_sha256==='string' && /^[a-f0-9]{64}$/.test(value.source_sha256) &&
      typeof value.note_after==='string' && value.note_after.length<=100 &&
      typeof value.target==='string' && value.target.length<=80 &&
      typeof value.backend_code==='string' && value.backend_code.length<=80 &&
      typeof value.backend_method==='string' && value.backend_method.length<=40 && !!preview(value.preview_png_base64);
  }
  function node(tag,className,text) {
    const element=document.createElement(tag);
    if(className) element.className=className;
    if(text!==undefined) element.textContent=text;
    return element;
  }
  function render(value,container) {
    if(!accepts(value)) return false;
    const I=window.ShareletI18n,t=(key,args)=>I.t(key,args);
    const root=node('section','permission-demo');
    const header=node('div','pc-result-heading');
    header.append(node('span','pc-decision '+value.decision,t('pc_decision_'+value.decision)),node('span','pc-version','Permission Canvas '+value.plugin_version));
    root.append(header,node('p','pc-sandbox-note',t('pc_sandbox_note')));
    const codes={allowed:'pc_reason_allowed',blue_zone:'pc_reason_blue',black_zone:'pc_reason_black',
      approval_required:'pc_reason_yellow',clipboard_not_allowed:'pc_reason_clipboard',observed:'pc_reason_observed'};
    root.append(node('h2','pc-result-explanation',t(codes[value.backend_code] || 'pc_reason_blocked')));
    const figure=node('figure','pc-preview');
    const image=node('img');image.src=preview(value.preview_png_base64);image.alt=t('pc_preview_alt');image.width=800;image.height=480;
    figure.append(image,node('figcaption','',t('pc_preview_caption')));root.append(figure);
    const stats=node('dl','pc-stats');
    for(const [key,text] of [['pc_execution',String(value.execution_count)],['pc_target',I.content(value.target)],['pc_hidden','1']]) {
      const item=node('div');item.append(node('dt','',t(key)),node('dd','',text));stats.append(item);
    }
    root.append(stats);
    const note=node('div','pc-note-value');note.append(node('strong','',t('pc_note_after')),node('p','',value.note_after || t('pc_note_empty')));root.append(note);
    const legend=node('div','pc-legend');
    for(const level of levels) legend.append(node('span','pc-level '+level,t('pc_level_'+level)));
    root.append(legend);
    const details=node('details','pc-details');details.append(node('summary','',t('pc_details')));
    details.append(node('pre','',JSON.stringify({plugin:'Permission Canvas',version:value.plugin_version,
      source_sha256:value.source_sha256,backend_method:value.backend_method,backend_code:value.backend_code,
      execution_count:value.execution_count,pixels_masked:true,credential_exposed:false,sandbox:true},null,2)));
    root.append(details);
    container.replaceChildren(root);
    return true;
  }
  return {accepts,render};
})();
