# BUNKR

A self-hosted family preparedness hub. It runs on a small home server, keeps working without the internet, and
copies what matters onto a Raspberry Pi "field kit", a laptop and USB drives.

It's written in Python 3.12 using only the standard library (plus Ubuntu's `python3-cryptography`), with SQLite and a
plain HTML/JS phone-friendly web app. No pip packages, no cloud account.

## What it does

- **Supplies and readiness:**
  - Inventory by location and kit, with targets from Ready.gov's [Build a Kit](https://www.ready.gov/kit) list, scaled to your household.
  - Water and food counted in days; expiry tracking; a shopping list from your gaps.
  - Barcode quick-add and scenario checklists.
- **Family:**
  - Check-ins ("I'm safe / on my way / need help"), with battery level, and family calls ("meet at…", "evacuate to…"), with drills.
  - Medical cards and medicine supply; a printable family plan; a maintenance calendar.
- **Notifications:**
  - Phone push (Web Push, end-to-end encrypted), a notification inbox, and reminder emails through your own mail account.
  - Document renewal reminders.
- **Situation:**
  - NWS alerts and hazards for your places, wildfires, earthquakes, space weather, internet outages and cyber advisories, ranked by how close they are.
- **News:**
  - Headlines from many outlets and regions, grouped into stories and ranked for each person, plus a morning brief.
  - Early warning for hacks, outages and dangers near your people.
  - A news search for each family town, and a separate "Unconfirmed" view of public posts.
  - Anyone can flag a source that doesn't belong to its story, so the grouping learns.
- **Flights:** follow family flights (adsb.lol, FAA delays, optional FlightAware).
- **Mesh radio:**
  - Family check-ins and calls over a MeshCore companion radio (USB or Wi-Fi), with several channels.
  - A small command bot, and a list of known nodes by public key.
- **Offline kit:**
  - Encrypted document archives (7-Zip AES-256) and Kiwix offline libraries (medicine, repair, Wikipedia).
  - Offline maps, radio programming files (CHIRP), and firmware for mesh boards.
  - All of it on USB drives and on field nodes.
- **Field nodes:** a Pi or laptop syncs from the hub and serves a read-only field page and check-ins over its own Wi-Fi.
  Optional "node lock" keeps its database encrypted until it's back home.
- **Security:**
  - Logins, passkeys, two-step login and an audit log.
  - Firearms and serial numbers are admin-only and never leave the hub.
- **Optional extras:** Home Assistant.
- **Optional AI, off by default:** AI summaries in the morning brief, and an AI connector (MCP). See below.

The offline app works on phones as an installed web app (Add to Home Screen) and keeps the last data it saw when
the connection drops.

## Run it

Try it on one computer:

```
./run.sh                       # or: python3 -m bunkr
```

It opens http://127.0.0.1:8765 and only listens on that computer. Data goes in `~/.local/share/bunkr/bunkr.db`.

For a hub, a Raspberry Pi field node or a laptop node, see [deploy/INSTALL.md](deploy/INSTALL.md).

Requirements (Ubuntu or Debian): `python3` (3.12), `python3-cryptography`, `7zip`, `rsync`. Optional: `tesseract-ocr`, to read calories off nutrition label photos.

## Keys and outside services

Everything works without accounts. Some extras need your own free or paid keys, saved on the hub in files only
BUNKR can read:
- NASA FIRMS (wildfires) and Cloudflare Radar (internet outages);
- FlightAware AeroAPI (flight times);
- a mail account (reminder emails);
- Home Assistant;
- Kroger's product catalog (barcode lookups for Kroger-family stores and store brands);
- USDA FoodData Central (works without a key; a free api.data.gov key allows more lookups);
- Reddit (only with Reddit's approval).

Each install uses its own keys, and BUNKR never shares data with anyone else.

## AI is optional

BUNKR doesn't need AI or any AI subscription. A new install has it off, and every feature works without it. If you
want it, one card on the Household tab ("AI (optional)") turns on:
- **AI summaries:** the morning brief says where outlets agree and differ, and news stories get sorted by place and topic.
- **AI connector (MCP):** an AI assistant can read your inventory and add or move items, after you sign in. Never firearms.

It can run on a free local model (Ollama, on your own computer) or a command-line AI tool on the hub. Nothing else
in BUNKR uses AI.

## Versions and upgrades

Releases are numbered MAJOR.MINOR.PATCH and listed in [CHANGELOG.md](CHANGELOG.md) and on the GitHub Releases page.
Once a day the hub checks for a newer release and tells its admins; it never updates itself. Upgrade when you
choose: see "Upgrading" in [deploy/INSTALL.md](deploy/INSTALL.md). The check can be turned off on the Household tab.

## Tests

```
python3 -m unittest discover -s tests
```

## Design

[DESIGN.md](DESIGN.md) explains how the pieces fit together.

## Support BUNKR

BUNKR is free and stays free. If it helps your family and you'd like to say thanks, you can sponsor it on
[GitHub Sponsors](https://github.com/sponsors/MrPatzy) or [buy me a coffee](https://buymeacoffee.com/zachpatzm).
Bug reports and pull requests help just as much.

## Disclaimer

BUNKR is a personal tool shared as-is. It is not an official alert service, not medical advice, and no
substitute for your local emergency services, official alerts (Wireless Emergency Alerts, NOAA Weather Radio,
your county's alert sign-up) or your own judgment. News and "Unconfirmed" posts can be wrong.

## Licence

Copyright (C) 2026 the BUNKR contributors.

BUNKR is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for details.

If you run a modified version for other people over a network, the AGPL asks you to offer them your source code.
