# Sharelet relay

A small HTTPS service that keeps Sharelet links stable across tunnel reconnects. Standard library only.

```sh
python3 server.py --db /var/lib/sharelet-relay/relay.sqlite3 --port 9580 --host 127.0.0.1
```

Put it behind an HTTPS reverse proxy that sets `X-Real-IP`, then point Sharelet at it with `relay_url: https://your-relay.example`.

What it stores per host: a random host ID, a hash of the host's secret, the current tunnel address and when it was last seen. Hosts idle for 60 days are removed. Link tokens stay in the URL fragment, so the relay never receives them. A tunnel address is accepted only after the relay has checked, through that address, that it belongs to the host.
