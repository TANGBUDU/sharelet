"use strict";
(() => {
  const copy = {
    en: {opening: "Opening the shared tool…", wait: "One moment.",
         offline: "The creator is offline right now", offline_body: "This tool runs on the creator's computer, which is off or reconnecting. This page will open the tool by itself as soon as it is back.",
         retry: "Checking again every 15 seconds.", broken: "This link is incomplete", broken_body: "Ask the creator to send the full link again.",
         missing: "This link is no longer available", missing_body: "The creator may have removed this Sharelet address."},
    zh: {opening: "正在打开分享的工具…", wait: "请稍候。",
         offline: "创作者暂时离线", offline_body: "这个工具运行在创作者的电脑上，电脑现在关着或正在重连。等它恢复后，本页面会自动打开工具。",
         retry: "每 15 秒自动重试一次。", broken: "链接不完整", broken_body: "请让创作者重新发送完整链接。",
         missing: "这个链接已失效", missing_body: "创作者可能已经停用了这个 Sharelet 地址。"},
    ja: {opening: "共有されたツールを開いています…", wait: "少々お待ちください。",
         offline: "作成者は現在オフラインです", offline_body: "このツールは作成者のパソコンで動いています。電源が切れているか再接続中です。戻り次第、このページが自動でツールを開きます。",
         retry: "15 秒ごとに再確認しています。", broken: "リンクが不完全です", broken_body: "作成者に完全なリンクを送り直してもらってください。",
         missing: "このリンクは利用できません", missing_body: "作成者がこの Sharelet アドレスを削除した可能性があります。"}
  };
  const lang = (navigator.languages || [navigator.language || "en"]).map(value => String(value).toLowerCase().slice(0, 2)).find(value => copy[value]) || "en";
  const text = copy[lang];
  document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;
  const show = (title, message, hint) => {
    document.getElementById("title").textContent = title;
    document.getElementById("message").textContent = message;
    const node = document.getElementById("hint"); node.hidden = !hint; node.textContent = hint || "";
    document.title = title + " · Sharelet";
  };
  show(text.opening, text.wait);
  const host = (location.pathname.match(/^\/h\/([a-f0-9]{32})\/?$/) || [])[1];
  const token = location.hash.slice(1);
  if (!host || !/^[a-f0-9]{24}\.[A-Za-z0-9_-]{43}$/.test(token)) { show(text.broken, text.broken_body); return; }
  async function attempt() {
    try {
      const response = await fetch("/api/hosts/" + host, {cache: "no-store"});
      if (response.status === 404) { show(text.missing, text.missing_body); return; }
      const value = await response.json();
      if (value.online && /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(value.origin || "")) {
        location.replace(value.origin + "/s/" + token);
        return;
      }
    } catch {}
    show(text.offline, text.offline_body, text.retry);
    setTimeout(attempt, 15000);
  }
  attempt();
})();
