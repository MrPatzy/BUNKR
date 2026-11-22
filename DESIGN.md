# How BUNKR fits together

## Roles

- **Hub:** a small always-on home server, such as a container on a home server or a mini PC. It holds the main database, fetches public feeds, sends notifications, and builds the offline kit.
- **Field node:** a Raspberry Pi, or a laptop (`--role pi` / `--role laptop`). It syncs records and downloads from the hub every hour while it can reach it, then works on its own:
  - It serves a field page and check-ins over its own Wi-Fi hotspot.
  - Phones on that hotspot need no login for the read-only page; the Wi-Fi password is the gate.
  - With `--lock`, its database is kept encrypted until it's unlocked at home or with the family passphrase.
- **Standalone:** everything on one computer, listening only on localhost.

## Code

- **Where things are:**
  - `bunkr/server.py`: the HTTP server, API and background scheduler. `bunkr/static/` holds the web app.
  - `bunkr/db.py`: SQLite storage. Schema changes are numbered migrations; an old one is never edited.
  - One module per area: `checkins`, `medical`, `situation`, `news`, `brief`, `flights`, `mesh`/`meshcore`/`meshbot`, `localwatch`, `notify`, `mailer`, `renewals`, `sync`, `builder` (USB kit), `vault` (node lock), `mfa`, `audit`, and others.
- **Dependencies:** the standard library only, plus `python3-cryptography`, imported inside the functions that need it. Nothing makes a network call at import time.
- **Secrets:** keys and tokens are files next to the database (mode 600). They never go in the database, the API, sync or logs.

## Optional AI

- **Off by default:** a new install uses no AI anywhere, and every feature works without it.
- **One switch:** the "AI (optional)" card on the Household tab turns on, separately, AI summaries (the morning brief's
  write-ups and sorting news stories by place and topic) and the AI connector (MCP). Nothing else uses AI.
- **Where it runs:** a local model through Ollama (free, stays on your network), or a command-line AI tool on the hub
  (`--ai-cli`). All of it goes through `bunkr/ai.py`.
- **Safety:** text in, text out. Headlines are fenced as other people's words; replies are parsed and checked, never run.
  The connector is narrow (`bunkr/mcp_tools.py`): no firearms, no deleting what it didn't add, every change undoable.

## Notifications

- **Who gets what:** `notify.plan` decides, by event, who gets a push. It follows calm rules: life-safety always comes through; "soon / near you" alerts respect quiet hours and a daily cap; and repeats collapse into one.
- **The inbox (the bell):** everything a person would be notified about also goes to their in-app inbox, quiet hours or not.
- **Email:** reminders can go by email (each person picks notification, email or both). Mail waits in an outbox and is retried for three days.

## News and early warning

- **Grouping:** headlines from labelled outlets (country, region, owner) are grouped into stories by shared rare words. When a flagged mismatch is confirmed, the words behind it count for less from then on.
- **Check speed:** wires, breaking-news, specialist (cyber, government IT, farm) and local TV feeds are checked every 10 minutes; the rest hourly.
- **Early warning:** a new story whose headline says hack, outage, boil water, evacuation and the like is raised early:
  - to everyone when it's in the configured country or state;
  - as "Near <person> (<town>)" when it names a town on the Sites tab where someone lives.
- **Local watch:** each family town also gets its own news search.
- **Unconfirmed:** public posts (Bluesky, and Reddit with an approved key) that name a family town, mention trouble and have a local clue go to a separate "Unconfirmed" view. Two different people posting the same thing raises a heads-up; posts the news later covers are marked.

## Mesh radio

- **Connection:** BUNKR talks to a MeshCore companion radio over USB serial or TCP, using the companion protocol.
- **Channels:** each one has a kind.
  - `family`: check-ins, calls, help relays and the full bot.
  - `group`: a trusted circle, with check-ins and basic commands only.
- **Who sent it:** channel messages carry only the sender's node name. BUNKR keeps known nodes by public key and lets an admin say whose each one is.

## Offline phone copy

The service worker caches the app and the last data the API marked as safe to keep (never firearms). A 401 or
logout clears it.

## Sensitive data

- **Firearms:** firearms and serial numbers are admin-only. They're stripped from sync, the AI connector, the offline cache and family views.
- **The audit log** records logins, changes, and who looked at firearms, documents, medical details and printouts.
