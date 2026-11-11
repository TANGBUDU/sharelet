# Sharelet 0.5.1

Date: 2026-11-12. Catalog build of 0.5.0. The optional built-in image provider now uses only the key entered in Sharelet or the host's `OPENAI_API_KEY`; it no longer reads other credential files. The weather presentation has its own card title in all three languages, and the share list no longer mentions the removed Go-live button. A real Hermes profile loaded Sharelet with the unchanged Weather 2.1.0 plugin, and the demo video was recorded from that run through the public relay.

## Sharelet 0.5.0 validation

Date: 2026-11-11. This release simplifies sharing to three steps and makes links survive reconnects.

- **144 tests passed on Windows.** New checks cover the relay store and HTTP service, the host's relay client, stable links issued before the tunnel opens, falling back to the tunnel address until a relay has accepted the host once, local-only links that never open it, automatic reconnect with backoff, link defaults (20 runs, 7 days) and bounds (1,000 runs, 30 days). Linux was not rerun for this release.
- The installable archive cold-started with third-party site-packages disabled, and a second process attached to it.
- An isolated preview (not the creator's live service) ran the new creator page in Chinese, in a real browser: choosing a tool showed one summary line, one confirmation for a tool without a read-only hint and a collapsed **More settings**. The link was issued at once as a relay link. A loopback test relay and a **real Cloudflare quick tunnel** were used: the tunnel opened by itself, the relay verified it through the guest `/healthz`, and the link forwarded a guest browser to the guest page, where a run returned readable text instead of raw JSON.
- Killing the preview's tunnel process made Sharelet reconnect by itself with a new temporary address. The **same link** then opened the guest page at the new address. A host that had never come online showed the translated "creator offline" page, which keeps retrying.
- The relay never receives link tokens: they stay in the URL fragment, and the landing page sends them only to the creator's verified address.
- The public relay is deployed at `https://sharelet.109-123-230-235.sslip.io` behind nginx with a Let's Encrypt certificate, as a dedicated system user under a hardened systemd unit. HTTP redirects to HTTPS, and pages carry the strict CSP, `no-referrer` and `X-Frame-Options: DENY`. No other site's configuration was changed.
- An isolated host with a fresh identity and a **real Cloudflare quick tunnel** then ran against that public relay. The relay accepted the host, and the first share was issued at once as a public relay link. The relay reported the host online at its tunnel address, and a guest run there returned the tool's result. Killing the tunnel process made the relay point the **same link** at a new address within the reconnect window, where a second run succeeded. After the host shut down, the relay reported it offline.
- Not yet verified: a Hermes run of this version, and Linux.
