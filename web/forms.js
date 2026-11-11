"use strict";
window.ShareletForms = (() => {
  function input(name, field, value, id, required) {
    const e = esc;
    const mandatory = required && !field.nullable && !(field.type === 'string' && field.minLength === 0);
    const attrs = ' id="' + e(id) + '" aria-label="' + e(I.content(field.title || name)) + '" data-i18n-content-aria-label="'+e(field.title || name)+'" data-form-field="' + e(name) + '" data-required="' + mandatory + '"' + (mandatory ? ' required' : '');
    if(field.format==='sharelet-pdf-uploads') {
      return '<div class="file-picker" data-file-field="'+e(name)+'"><input type="hidden"'+attrs+' value="">'+
        '<label class="file-drop" for="'+e(id)+'-upload"><span>'+icon('plus')+'</span><strong data-i18n="file_choose">'+e(t('file_choose'))+'</strong><small data-i18n="file_limits">'+e(t('file_limits'))+'</small></label>'+
        '<input class="file-native" type="file" id="'+e(id)+'-upload" multiple accept="application/pdf,.pdf" data-file-picker aria-label="'+e(t('file_choose'))+'" data-i18n-aria-label="file_choose">'+
        '<div class="file-list" aria-live="polite"></div><p class="field-help" data-file-status></p></div>';
    }
    if (field.enum) {
      const selected = value === undefined ? '' : String(field.enum.findIndex(item => typeof item === typeof value && item === value));
      return '<select' + attrs + '><option value="" data-i18n="choose_value">' + e(t('choose_value')) + '</option>' + field.enum.map((item, index) =>
        '<option value="' + index + '"' + (String(index) === selected ? ' selected' : '') + (item===null ? ' data-i18n="no_value"' : typeof item==='boolean' ? ' data-i18n="'+(item ? 'yes' : 'no')+'"' : ' data-i18n-content="'+e(item)+'"')+'>' + e(item===null ? t('no_value') : typeof item==='boolean' ? t(item ? 'yes' : 'no') : I.content(item)) + '</option>').join('') + '</select>';
    }
    if (field.type === 'boolean') {
      return '<select' + attrs + '><option value="" data-i18n="'+(!required ? 'not_provided' : field.nullable ? 'no_value' : 'choose_value')+'">' + e(t(!required ? 'not_provided' : field.nullable ? 'no_value' : 'choose_value')) + '</option>' +
        '<option value="true" data-i18n="yes"' + (value === true ? ' selected' : '') + '>' + e(t('yes')) + '</option>' +
        '<option value="false" data-i18n="no"' + (value === false ? ' selected' : '') + '>' + e(t('no')) + '</option></select>';
    }
    if (field.type === 'integer' || field.type === 'number') {
      return '<input' + attrs + ' type="number" min="' + field.minimum + '" max="' + field.maximum + '" step="' +
        (field.type === 'integer' ? '1' : 'any') + '" value="' + e(value == null ? '' : value) + '">';
    }
    return '<textarea' + attrs + ' rows="3" minlength="' + (field.minLength || 0) + '" maxlength="' +
      (field.maxLength || 500) + '" data-i18n-content-placeholder="'+e(field.description || '')+'" placeholder="' + e(I.content(field.description || '')) + '">' + e(value == null ? '' : value) + '</textarea>';
  }
  function readValue(control, field, name, required) {
    const raw = control.value;
    if(field.format==='sharelet-pdf-uploads') {
      try {const files=JSON.parse(raw);if(!Array.isArray(files)||!files.length)throw new Error();}
      catch {throw new Error(t('file_required'));}
      return raw;
    }
    if (raw === '' && field.nullable && required) return null;
    if (raw === '' && !required) return undefined;
    if (field.enum) {
      if (raw === '' || !Number.isInteger(Number(raw)) || !Object.hasOwn(field.enum, Number(raw))) throw I.formError(field.title || name);
      return field.enum[Number(raw)];
    }
    if (field.type === 'boolean') {
      if (!['true', 'false'].includes(raw)) throw I.formError(field.title || name);
      return raw === 'true';
    }
    if (field.type === 'number' || field.type === 'integer') {
      if (raw === '' || !Number.isFinite(Number(raw))) throw I.formError(field.title || name);
      const value = Number(raw);
      if (field.type === 'integer' && !Number.isInteger(value)) throw I.formError(field.title || name);
      return value;
    }
    return raw;
  }
  function guest(schema, container, previous = {}) {
    container.innerHTML = Object.entries(schema.properties).map(([name, field], index) => {
      const id = 'guest-field-' + index;
      const value = Object.hasOwn(previous, name) ? previous[name] : field.default;
      return '<div class="input-group guest-field"><label class="field-label" for="' + id + '"><span data-i18n-content="'+esc(field.title || name)+'">' + esc(I.content(field.title || name)) + '</span>'+
        (!schema.required.includes(name) ? ' <span class="muted" data-i18n="optional">' + esc(t('optional')) + '</span>' : '') + '</label>' +
        input(name, field, value, id, schema.required.includes(name)) +
        (field.description ? '<p class="field-help" data-i18n-content="'+esc(field.description)+'">' + esc(I.content(field.description)) + '</p>' : '') + '</div>';
    }).join('');
  }
  function values(schema, container) {
    const result = {};
    for (const [name, field] of Object.entries(schema.properties)) {
      const control = Array.from(container.querySelectorAll('[data-form-field]')).find(item => item.dataset.formField === name);
      if (!control) continue;
      const value = readValue(control, field, name, schema.required.includes(name));
      if (value !== undefined) result[name] = value;
    }
    return result;
  }
  function fixed(schema, container) {
    container.innerHTML = Object.entries(schema.properties).map(([name, field], index) => {
      if(field.format==='sharelet-pdf-uploads') return '<div class="parameter-row" data-parameter="'+esc(name)+'"><div class="parameter-label"><strong data-i18n-content="'+esc(field.title || name)+'">'+esc(I.content(field.title || name))+'</strong><select class="parameter-mode" disabled><option value="guest" data-i18n="guest_fills">'+esc(t('guest_fills'))+'</option></select></div><div class="parameter-value" hidden></div><p class="field-help" data-i18n="file_creator_help">'+esc(t('file_creator_help'))+'</p></div>';
      const locked = Object.hasOwn(field, 'const'), value = locked ? field.const : field.default;
      return '<div class="parameter-row" data-parameter="' + esc(name) + '"><div class="parameter-label"><strong data-i18n-content="'+esc(field.title || name)+'">' + esc(I.content(field.title || name)) +
        '</strong><select class="parameter-mode" data-i18n-aria-label="parameter_mode" data-i18n-args="'+esc(JSON.stringify({name:field.title || name}))+'" aria-label="' + esc(t('parameter_mode', {name:I.content(field.title || name)})) + '"' + (locked ? ' disabled' : '') + '>' +
        '<option value="guest" data-i18n="guest_fills"' + (!locked ? ' selected' : '') + '>' + esc(t('guest_fills')) + '</option><option value="fixed" data-i18n="keep_fixed"' +
        (locked ? ' selected' : '') + '>' + esc(t('keep_fixed')) + '</option></select></div><div class="parameter-value"' + (!locked ? ' hidden' : '') + '>' +
        input(name, field, value, 'fixed-field-' + index, true) + '</div>' +
        (field.description ? '<p class="field-help" data-i18n-content="'+esc(field.description)+'">' + esc(I.content(field.description)) + '</p>' : '') + '</div>';
    }).join('');
    function update() {
      for (const row of container.querySelectorAll('[data-parameter]')) {
        const mode = row.querySelector('.parameter-mode').value;
        row.querySelector('.parameter-value').hidden = mode !== 'fixed';
        for (const control of row.querySelectorAll('[data-form-field]')) control.disabled = mode !== 'fixed';
      }
    }
    container.onchange = update;
    update();
  }
  function fixedValues(schema, container) {
    const result = {};
    for (const row of container.querySelectorAll('[data-parameter]')) {
      const name = row.dataset.parameter, field = schema.properties[name];
      if (row.querySelector('.parameter-mode').value === 'fixed') result[name] = readValue(row.querySelector('[data-form-field]'), field, name, true);
    }
    return result;
  }
  return {input, guest, values, fixed, fixedValues};
})();
