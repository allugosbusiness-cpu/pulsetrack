# PulseTrack — Running on Public / LAN Addresses

All three apps are configured to bind to **all network interfaces** (`0.0.0.0`), so
they are reachable from other devices on the LAN and (with a public tunnel / port
forward) from the internet.

## Services running now
| App | URL (LAN) | URL (local) |
|-----|-----------|-------------|
| Django API backend | http://10.168.166.180:8000/api/v1/ | http://127.0.0.1:8000/api/v1/ |
| Web frontend (Vite) | http://10.168.166.180:5173/ | http://localhost:5173/ |
| Android app (Expo/Metro) | exp://10.168.166.180:8081 | exp://localhost:8081 |

The machine also has a public IP: `14.194.100.206`, but the router does **not**
currently forward external ports to this PC, so that IP answers only from inside
the network until port forwarding is set up.

## What was changed
- **Backend** runs `python server/manage.py runserver 0.0.0.0:8000`
  - `ALLOWED_HOSTS` accepts these hosts; CORS is wide open in dev (see
    `server/Logistics/settings.py`).
- **Web frontend** runs `vite --host 0.0.0.0 --port 5173`
  - `client/Frontend/.env.local` sets `VITE_API_BASE_URL=http://10.168.166.180:8000/api`
- **Android app (Expo)** runs `npx expo start --lan --port 8081`
  - `pulsetrack-mobile/src/config/api.js` sets
  `FLEET_BACKEND_HOST = '10.168.166.180'` (port 8000)

## How to run each app
Backend:
```
.venv-back\Scripts\python server\manage.py runserver 0.0.0.0:8000
```
Web:
```
cd client/Frontend
npm install
npm run dev -- --host 0.0.0.0
```
Android (Expo):
```
cd pulsetrack-mobile
npm install
# IMPORTANT: pin the packager host to the PC's REACHABLE network IP so the
# QR / manifest tells Expo Go to download the bundle from THIS PC (not localhost).
export REACT_NATIVE_PACKAGER_HOSTNAME=10.168.166.180   # PowerShell: $env:REACT_NATIVE_PACKAGER_HOSTNAME='10.168.166.180'
npx expo start --lan
```
Then in the Expo Go app on the device: scan the QR shown in terminal, or open
`exp://10.168.166.180:8081`.

> **If Expo Go says "failed to download remote update"**: this almost always means
> the manifest told the phone to fetch the bundle from `127.0.0.1` (the *phone*,
> not the PC). Setting `REACT_NATIVE_PACKAGER_HOSTNAME` to the PC's reachable IP
> (or using `--lan` when the phone can route there) fixes it.

## Reaching the apps from a REAL public IP (outside the LAN)
Because this router/ISP does not forward ports, you need one of:
1. **Port forward on the router** to this PC for ports `8000` (backend),
   `5173` (web), `8081` (Expo), then use `http://14.194.100.206:<port>`.
2. **Tunnel (recommended, no router access needed)** — run
   `npx ngrok http 8000`, and similarly for `5173` / `8081` if you need them
   public. Put the ngrok https URL into:
   - `client/Frontend/.env.local` → `VITE_API_BASE_URL=<ngrok>/api`
   - `pulsetrack-mobile/src/config/api.js` → `FLEET_BACKEND_HOST=<ngrok host>`,
     `FLEET_BACKEND_PORT=443`, `API_BASE_URL = \`https://<ngrok host>/api/v1\``
   - Restart the servers after changing.

Note: the machine's own firewall (`netsh advfirewall firewall add rule ...`)
must permit inbound TCP on any ports you expose.