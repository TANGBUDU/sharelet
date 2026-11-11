"use strict";
window.ShareletFiles=(()=>{
  const active=new WeakMap();
  function isFileSchema(schema){return Object.values(schema?.properties||{}).some(field=>field.format==='sharelet-pdf-uploads');}
  function size(bytes){return bytes>=1048576?(bytes/1048576).toFixed(2)+' MB':Math.max(1,Math.round(bytes/1024))+' KB';}
  async function response(request){
    const res=await request;let value;try{value=await res.json();}catch{throw new Error(t('connection_lost'));}
    if(!res.ok || !value.ok){const error=new Error(value.error || t('loading_failed'));error.status=res.status;throw error;}
    return value;
  }
  function bind(container,base,{busy,error,schema}){
    const action=container.querySelector('[data-form-field="action"]'),pages=container.querySelector('[data-form-field="pages"]');
    if(action && pages && schema.properties.action?.enum?.includes('Extract pages')) {
      const visibility=()=>{
        const show=schema.properties.action.enum[Number(action.value)]==='Extract pages',field=pages.closest('.guest-field');
        field.hidden=!show;pages.dataset.required=String(show);pages.required=show;
        const optional=field.querySelector('[data-i18n="optional"]');if(optional)optional.hidden=show;
      };
      action.addEventListener('change',visibility);visibility();
    }
    for(const box of container.querySelectorAll('[data-file-field]')) {
      const state={items:[],working:false,running:false};active.set(box,state);
      const hidden=box.querySelector('[data-form-field]'),picker=box.querySelector('[data-file-picker]'),list=box.querySelector('.file-list'),status=box.querySelector('[data-file-status]');
      function render(){
        hidden.value=JSON.stringify(state.items.map(file=>file.id));
        picker.disabled=state.working||state.running;
        list.replaceChildren();
        for(const [index,file] of state.items.entries()) {
          const row=document.createElement('div');row.className='file-row';
          const title=document.createElement('div');title.className='file-name';
          const name=document.createElement('strong');name.textContent=file.name;
          const detail=document.createElement('small');detail.textContent=size(file.bytes);title.append(name,detail);row.append(title);
          const controls=document.createElement('div');controls.className='file-controls';
          for(const [kind,glyph,disabled] of [['up','↑',index===0],['down','↓',index===state.items.length-1],['remove','×',false]]) {
            const button=document.createElement('button');button.type='button';button.className='file-control';button.textContent=glyph;
            button.setAttribute('aria-label',t('file_'+kind,{name:file.name}));button.disabled=disabled||state.working||state.running;
            button.onclick=async()=>{
              if(state.working||state.running)return;
              if(kind==='remove'){
                state.working=true;busy(true);render();
                try{await response(fetch(base+'/uploads/'+file.id+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}));state.items.splice(index,1);}
                catch(reason){if(reason.status===404)state.items.splice(index,1);else error(reason);}
                finally{state.working=false;busy(false);render();}
              }else{
                const destination=index+(kind==='up'?-1:1);[state.items[index],state.items[destination]]=[state.items[destination],state.items[index]];render();
              }
            };controls.append(button);
          }
          row.append(controls);list.append(row);
        }
        status.textContent=state.working?t('file_uploading'):state.items.length?t('file_count',{count:state.items.length}):'';
      }
      picker.onchange=async()=>{
        const selected=Array.from(picker.files||[]);if(!selected.length || state.working || state.running)return;
        if(selected.length+state.items.length>8 || selected.some(file=>file.size>5*1048576 || !file.name.toLowerCase().endsWith('.pdf')) || selected.reduce((sum,file)=>sum+file.size,0)+state.items.reduce((sum,file)=>sum+file.bytes,0)>20*1048576){error(new Error(t('file_limits')));picker.value='';return;}
        state.working=true;busy(true);render();
        try{
          for(const file of selected){
            const value=await response(fetch(base+'/uploads',{method:'POST',headers:{'Content-Type':'application/pdf','X-Sharelet-Filename':encodeURIComponent(file.name)},body:file}));
            state.items.push(value.upload);render();
          }
        }catch(reason){error(reason);}
        finally{state.working=false;picker.value='';busy(false);render();}
      };
      state.render=render;render();
    }
  }
  function clear(container){
    for(const box of container.querySelectorAll('[data-file-field]')){
      const state=active.get(box);if(state)state.items=[];
      box.querySelector('[data-form-field]').value='[]';box.querySelector('.file-list').replaceChildren();box.querySelector('[data-file-status]').textContent='';
    }
  }
  function refresh(container){
    for(const box of container.querySelectorAll('[data-file-field]')){
      const state=active.get(box);if(!state)continue;
      const buttons=box.querySelectorAll('.file-control');
      for(const [index,file] of state.items.entries())for(const [offset,kind] of ['up','down','remove'].entries())buttons[index*3+offset].setAttribute('aria-label',t('file_'+kind,{name:file.name}));
      box.querySelector('[data-file-status]').textContent=state.working?t('file_uploading'):state.items.length?t('file_count',{count:state.items.length}):'';
    }
  }
  function result(job,container){
    const value=job.result;
    if(!value || value.kind!=='file' || value.content_type!=='application/pdf' || !job.file_url)return false;
    const make=(tag,className,text)=>{const node=document.createElement(tag);node.className=className;if(text!==undefined)node.textContent=text;return node;};
    const root=make('div','file-result');
    root.append(make('div','file-success-mark','✓'),make('span','eyebrow',t('file_ready')),make('h2','',value.filename),make('p','file-success-description',t('file_ready_description')));
    const stats=make('dl','file-result-stats');
    for(const [label,text] of [['file_pages',String(value.pages)],['file_before',size(value.input_bytes)],['file_after',size(value.bytes)]]){
      const item=make('div','');item.append(make('dt','',t(label)),make('dd','',text));stats.append(item);
    }
    root.append(stats);
    const link=make('a','button primary full-width',t('file_download'));link.href=job.file_url;link.download=value.filename;root.append(link);
    if(value.filename==='compressed.pdf')root.append(make('p','field-help',value.bytes<value.input_bytes?t('file_reduction',{percent:Math.round((1-value.bytes/value.input_bytes)*100)}):t('file_no_reduction')));
    root.append(make('p','field-help',t('file_reselect')));container.replaceChildren(root);return true;
  }
  function running(container,value){for(const box of container.querySelectorAll('[data-file-field]')){const state=active.get(box);if(state){state.running=value;state.render();}}}
  function validate(schema,values){
    if(!isFileSchema(schema) || !schema.properties.action?.enum?.includes('Extract pages'))return;
    const ids=JSON.parse(values.files||'[]'),action=values.action||schema.properties.action.default||'Merge PDFs';
    if(action==='Merge PDFs' ? ids.length<2 : ids.length!==1)throw new Error(t('file_error_05'));
    if(action==='Extract pages' && !values.pages?.trim())throw new Error(t('file_error_06'));
  }
  return {bind,clear,refresh,result,running,isFileSchema,validate};
})();
