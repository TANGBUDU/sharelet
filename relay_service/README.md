# Self-hosted Sharelet relay

An optional HTTPS service for stable Sharelet links across tunnel reconnects. Sharelet leaves `relay_url` empty by default and does not contact a relay until the creator configures one.

```sh
python3 server.py --db /var/lib/sharelet-relay/relay.sqlite3 --port 9580 --host 127.0.0.1
```

Put it behind an HTTPS reverse proxy that sets `X-Real-IP`, then set `relay_url: https://your-relay.example` in the creator's Sharelet settings.

It stores a random host ID, a hash of the relay secret, the current tunnel address and its last-seen time. The secret travels over HTTPS during registration and heartbeats. Hosts idle for 60 days are removed. A tunnel address is accepted only after the relay verifies the host's guest `/healthz` through it.

Link tokens stay in the URL fragment and are absent from HTTP requests to the relay API. The relay's landing-page JavaScript reads that fragment to forward guests. It is trusted code: operate the relay yourself and control the JavaScript served from its origin. A fragment does not protect against a malicious relay operator.
