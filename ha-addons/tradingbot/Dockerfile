FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Installs the tradingbot package straight from this repository's git history,
# sidestepping the Home Assistant add-on build-context limitation (a Dockerfile
# can only COPY files from its own add-on folder, not the rest of the repo).
#
# Pinned to a specific commit SHA, not just a branch name. `pip install
# git+URL@branch` is cached by Docker based on the literal Dockerfile text,
# not by what's actually at the remote branch tip right now -- so leaving
# this as a branch name means "Rebuild" can silently keep reusing an old
# cached layer and never pick up new commits. Bumping the SHA below is what
# actually forces a fresh fetch.
#
# IMPORTANT: after pushing new commits you intend to deploy, update
# TRADINGBOT_COMMIT to the new commit SHA, then use the add-on's "Rebuild"
# button (not just "Restart") in the Home Assistant UI.
ARG TRADINGBOT_COMMIT=73825af3818b7318c4d1133bb2b9c4c4775ce46e
RUN pip install --no-cache-dir \
    "tradingbot @ git+https://github.com/Stullee/TradingBot.git@${TRADINGBOT_COMMIT}"

WORKDIR /app
COPY _addon_options.py /app/_addon_options.py
COPY addon_entrypoint.py /app/addon_entrypoint.py
COPY addon_backtest_entrypoint.py /app/addon_backtest_entrypoint.py

CMD ["python", "addon_entrypoint.py"]
