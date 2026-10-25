# Dixon host migration (2026-10-25)

## Change
- **Previous host:** mac11 (`/Users/paul/project-dixon`)
- **Current host:** darkstar (`/home/paul/project-dixon`)
- Public UI: https://dixon.robonet.cc/ui/
- Mac copy left on disk; Mac API + cloudflared stopped after cutover.

## Runtime
- Python: system `/usr/bin/python3.12` venv at `.venv`
- API: `127.0.0.1:8765` (see `.env.dixon`)
- Tunnel: Linux amd64 `bin/cloudflared` with `TUNNEL_TOKEN` from `.cf_tunnel_token.env` (mode 600; derived from `.cf_tunnel_token`)

## systemd (user units for `paul`)
Linger enabled: `loginctl enable-linger paul`

| Unit | Role |
|------|------|
| `dixon-api.service` | FastAPI / uvicorn |
| `dixon-cloudflared.service` | Cloudflare tunnel connector |
| `dixon-daily.timer` | Weekdays 07:30 HKT → `scripts/daily_job.sh` |

### Start / stop / status
```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user start dixon-api dixon-cloudflared
systemctl --user stop dixon-cloudflared dixon-api
systemctl --user status dixon-api dixon-cloudflared dixon-daily.timer
journalctl --user -u dixon-api -u dixon-cloudflared -f
```

Logs also append to `logs/api.log` and `logs/cloudflared.log`.

## Secrets (never commit / never echo)
- `.env.dixon` — API host/port/key
- `.cf_tunnel_token` — raw tunnel token
- `.cf_tunnel_token.env` — `TUNNEL_TOKEN=...` for systemd EnvironmentFile

## Notes
- Mac had no Dixon crontab/LaunchAgent; daily job timer was added on darkstar.
- Do not run two connectors with the same tunnel token (Mac cloudflared must stay stopped).
