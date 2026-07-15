FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Installs the tradingbot package straight from this repository's git history,
# sidestepping the Home Assistant add-on build-context limitation (a Dockerfile
# can only COPY files from its own add-on folder, not the rest of the repo).
#
# IMPORTANT: update the ref below (after @) to "main" once this branch is
# merged, or to a release tag. Use the add-on's "Rebuild" button in the
# Home Assistant UI to pick up new commits pushed to that ref.
RUN pip install --no-cache-dir \
    "tradingbot @ git+https://github.com/Stullee/TradingBot.git@claude/automated-trading-bot-ib-86km6e"

WORKDIR /app
COPY addon_entrypoint.py /app/addon_entrypoint.py

CMD ["python", "addon_entrypoint.py"]
