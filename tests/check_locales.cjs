const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {
  window: {addEventListener() {}}, navigator: {language: 'zh-CN'},
  document: {documentElement: {}, body: {dataset: {}}, querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}},
  Event, localStorage: {getItem: () => null, setItem() {}}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/i18n.js', 'utf8').replace('  const supported =', '  window.__messages=messages; const supported ='), context);
const I = context.window.ShareletI18n;
assert.equal(I.language, 'en');
const dictionaries = context.window.__messages;
const keys = Object.keys(dictionaries.en).sort();
const placeholders = value => [...value.matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort();
for (const lang of ['en', 'zh', 'ja']) {
  assert.deepEqual(Object.keys(dictionaries[lang]).sort(), keys);
  for (const key of keys) {
    assert.ok(dictionaries[lang][key].trim());
    assert.deepEqual(placeholders(dictionaries[lang][key]), placeholders(dictionaries.en[key]));
  }
  I.setLanguage(lang);
  for (const key of ['request_body_timeout', 'link_upload_busy', 'link_file_change_rate_limit', 'link_upload_rate_limit',
                     'request_not_sent_refunded', 'share_ended_refunded', 'link_request_body_busy']) {
    const source = dictionaries.en[key];
    assert.equal(I.message(source), dictionaries[lang][key]);
    assert.equal(I.message({error: source, code: 'busy'}), dictionaries[lang][key]);
    assert.notEqual(I.message(source), I.t('error_generic'));
  }
  assert.equal(I.message('The share ended before this queued request started. It was not sent to the tool, and no run was charged.'), dictionaries[lang].share_ended_refunded);
}
console.log(JSON.stringify({ok: true, languages: ['en', 'zh', 'ja'], messages_per_language: keys.length, new_errors_translated: 7, old_refund_message_supported: true}));
