# Deploying on Home Assistant OS

This covers running TradingBot as a Home Assistant add-on on a Home
Assistant OS (HAOS) host, with IB Gateway running alongside it.

Two pieces, two different deployment mechanisms:

- **TradingBot itself** → a proper Home Assistant add-on (`ha-addons/tradingbot`),
  installed and configured entirely through the HA UI.
- **IB Gateway** → the community-maintained
  [`gnzsnz/ib-gateway-docker`](https://github.com/gnzsnz/ib-gateway-docker)
  image, run via its own `docker-compose.yml` (provided in `deploy/ib-gateway/`)
  rather than reinventing it as an add-on — see "Why not an add-on for IB
  Gateway too?" below.

## 1. Run IB Gateway (recommended: on the HAOS host itself)

Home Assistant OS is a locked-down appliance; by default there's no shell or
Docker access. To run an extra container alongside it:

1. Install the **"Advanced SSH & Web Terminal"** add-on from
   `Settings → Add-ons → Add-on Store` (official Home Assistant Community
   Add-ons repository — add it via `⋮ → Repositories` if not already present).
2. In that add-on's configuration, **disable "Protection mode"** and set a
   strong SSH password/key. This is what grants it access to the host's
   Docker daemon — required to run containers outside the add-on system.
3. Start it, open its **Terminal** (or SSH in), then:
   ```bash
   mkdir -p /root/ib-gateway && cd /root/ib-gateway
   # copy deploy/ib-gateway/docker-compose.yml and .env.example from this
   # repo into this directory (e.g. via the "File editor" or Samba add-on,
   # or `wget`/`curl` the raw files from GitHub), then:
   cp .env.example .env
   nano .env   # fill in TWS_USERID / TWS_PASSWORD, review TRADING_MODE=paper
   docker compose up -d
   docker compose logs -f   # watch it log in; approve 2FA on your phone if prompted
   ```
4. Verify it's listening: `ss -tlnp | grep -E '4001|4002'` should show IB
   Gateway bound to `127.0.0.1:4002` (paper) and `127.0.0.1:4001` (live).

Because the compose file uses `network_mode: host`, IB Gateway's API only
ever binds to this machine's own loopback interface — it is **never exposed
to your LAN**, regardless of what else is running. This is the main reason
this is the recommended setup over running IB Gateway on a separate device.

### Alternative: IB Gateway on a different always-on device

If you'd rather not touch HAOS's protection mode, run the same
`docker-compose.yml` on any other always-on Linux box on your network
instead (drop `network_mode: host` and use standard port publishing, e.g.
`ports: ["<that-device-LAN-IP>:4002:4004"]` for paper — see the port
mapping notes in the compose file's comments). Then point the TradingBot
add-on's `ib_host` option at that device's LAN IP instead of `127.0.0.1`.

**Security note:** the IB API socket has no authentication of its own beyond
the login already performed inside IB Gateway — anyone who can reach that
TCP port can place trades. If you expose it beyond loopback, restrict it
with your host firewall (e.g. `ufw allow from <homeassistant-ip> to any port 4002`)
so only the Home Assistant host can reach it.

## 2. Install the TradingBot add-on

1. In Home Assistant: `Settings → Add-ons → Add-on Store → ⋮ → Repositories`,
   add `https://github.com/Stullee/TradingBot`.
2. The "TradingBot (IB Day Trading)" add-on should appear — install it. The
   first install builds the Docker image (pulls the bot's code from this
   repo's git history), which takes a couple of minutes.
3. Open the add-on's **Configuration** tab. At minimum set:
   - `ib_host: 127.0.0.1`, `ib_port: 4002` (matches the setup above)
   - `symbols` to whatever tickers you want to trade
   - Leave `allow_live_trading: false` until you've watched it paper trade
4. Start the add-on. Watch the **Log** tab for connection/strategy activity.

See `ha-addons/tradingbot/DOCS.md` (also shown in the add-on's Documentation
tab in HA) for the full list of configuration options.

## Why not an add-on for IB Gateway too?

It would be more convenient to install IB Gateway the same one-click way.
The reason it isn't packaged as a custom add-on here: doing that safely
means wrapping `gnzsnz/ib-gateway-docker`'s own startup process (translating
the add-on's options into env vars, then handing off to their entrypoint),
and getting that hand-off wrong in a way that isn't obvious until you're
mid-incident is a bad trade-off for something that already has a
well-maintained, directly-documented Docker image. Running it via its own
compose file, exactly as its maintainers document, avoids that risk
entirely. If you'd like, this can be revisited later as a proper add-on
once the exact hand-off has been verified against a running container.
