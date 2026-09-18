# Metered.ca TURN Server Setup — Manual Action Plan

## 1. Create Metered.ca Account

1. Go to https://www.metered.ca/signup
2. Sign up (email + password or GitHub OAuth)
3. Confirm email
4. You land on the Dashboard — free trial gives **500 MB** of TURN relay bandwidth (no monthly fee, no overages)

## 2. Create a TURN Server Application

1. Dashboard → **TURN Servers** → **Create**
2. Name: `shmax-production` (or any label)
3. Region: pick **Europe (Frankfurt)** — best latency for Russia ↔ EU path
4. Click **Create**
5. The app page shows your **API Key** and server URLs — keep this tab open

## 3. Get Credentials

On the app page you'll see:

| Field | Example value |
|---|---|
| **STUN URL** | `stun:a]stun.metered.ca:80` |
| **TURN URL (UDP)** | `turn:a]global.relay.metered.ca:80` |
| **TURN URL (TCP)** | `turn:a]global.relay.metered.ca:80?transport=tcp` |
| **TURNS URL (TLS/443)** | `turns:a]global.relay.metered.ca:443?transport=tcp` |
| **Username** | (shown on the page, a hex string) |
| **Credential** | (shown on the page, a hex string) |

> Replace `a]` with your actual app prefix shown in the dashboard.

**Important:** The `turns:...:443` URL is the critical one — it tunnels through firewalls that block non-HTTPS ports. Always include it.

## 4. Configure Production Environment

Set these env vars on the VPS (in `.env` or systemd environment file):

```bash
STUN_SERVERS=stun:a]stun.metered.ca:80,stun:stun.l.google.com:19302
TURN_SERVER_URL=turn:a]global.relay.metered.ca:80?transport=udp,turn:a]global.relay.metered.ca:80?transport=tcp,turns:a]global.relay.metered.ca:443?transport=tcp
TURN_SERVER_USERNAME=<your-username-from-dashboard>
TURN_SERVER_CREDENTIAL=<your-credential-from-dashboard>
```

> Replace `a]` and credentials with actual values from Step 3.

### Why multiple TURN URLs?

| Transport | Port | Purpose |
|---|---|---|
| UDP | 80 | Fastest, works on most networks |
| TCP | 80 | Fallback when UDP is blocked |
| TLS (TURNS) | 443 | Looks like HTTPS — passes through strict firewalls and DPI in Russia |

## 5. Verify Setup

### Quick test (before deploying code changes)

Use Metered.ca's built-in **TURN Test** page on the dashboard — it lets you test connectivity from your current network.

### After code changes are deployed

1. Open Shmax on two devices (one with Russian IP / VPN, one without)
2. Initiate a video call
3. Check browser DevTools → `chrome://webrtc-internals/`:
   - Look for `relay` in the ICE candidate pair — confirms TURN is being used
   - If you see `srflx` or `host` — that's direct/STUN (also fine, means TURN wasn't needed)
4. Test with a Russian mobile ISP (most aggressive NAT) — call should connect via TURN relay

### Test TURN from CLI

```bash
turnutils_uclient -t -u <username> -w <credential> a]global.relay.metered.ca
```

(Requires `coturn` package installed locally for `turnutils_uclient`.)

## 6. Monitor Usage

- Dashboard → **Usage** shows bandwidth consumed
- Free trial: **500 MB total** (not monthly — one-time quota, no overages)
- 1:1 video call at 480p ≈ 1–2 GB/hour through relay
- 500 MB ≈ **15–30 minutes** of relayed video — enough to verify the setup works, not for daily use

## 7. Cost Considerations

| Plan | Included TURN | Monthly fee | Overage |
|---|---|---|---|
| Free Trial | 500 MB (one-time) | $0 | None (stops working) |
| Growth | 150 GB/month | $99/mo | $0.40/GB |
| Business | 500 GB/month | $199/mo | $0.20/GB |
| Enterprise | 2 TB/month | $499/mo | $0.10/GB |

### Recommendation

The free trial is only useful for **testing the integration**. For actual daily use:
- **Option A:** Growth plan ($99/mo) — 150 GB is ~75–150 hours of relayed calls per month. Overkill for personal use but the cheapest paid tier.
- **Option B (better for personal use):** Self-host **coturn** on your existing VPS — $0 extra cost, unlimited bandwidth. The VPS already runs the Shmax backend, so adding coturn is straightforward. See separate plan if you want to go this route.
- **Option C:** Use Metered.ca free trial to validate the code changes, then switch env vars to a self-hosted coturn for production.

## Files That Need Code Changes (Task #14-s7ogup)

These are handled by the dev task, listed here for reference:

| File | Change |
|---|---|
| `backend/app/config.py:47-50` | Already has TURN env vars — may need to support multiple TURN URLs (comma-separated) |
| `frontend/services/webrtc.ts:10-13` | Replace hardcoded Google STUN with dynamic config from backend |
| `backend/app/routers/` | Add endpoint or WebSocket payload to deliver ICE config to frontend |
| `.env.example` | Add Metered.ca placeholder values with comments |
