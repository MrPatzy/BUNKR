# Changelog

BUNKR uses version numbers MAJOR.MINOR.PATCH. The hub tells admins when a newer release is out; upgrading is
always your choice (see "Upgrading" in deploy/INSTALL.md).

## Unreleased

## 1.2.0 (2026-11-24)

- **Check someone else in properly.** "Check in for them" on the family board now opens the same Safe / On the way / Needs help buttons you have for yourself, with a note and a flight number, so you can put someone on a flight and BUNKR follows it for them. No more typing the status.
- **Mesh bot: weather anywhere.** `WX` gives weather at home now, today and tonight in one message; `!wx 80202` or `!wx Denver CO` for any U.S. place, and `!wx 80202 5` for 3, 5 or 7 days (the weather service gives 7, so 10 says so). `!alerts 80202` lists the weather service's alerts there (red flag, winter storm, flood, air quality...) plus fires, quakes and other alerts BUNKR knows of within 50 miles.
- **Mesh bot: sun and moon.** `SUN` or `!sun 80202`: sunrise, sunset, daylight length, first and last light, and the moon's phase. Worked out on the hub, no internet needed.
- **Mesh bot: earthquakes and space weather.** `QUAKE` (or `!quake 80202`): the biggest quakes within 300 miles in the last day. `SPACE`: radio blackouts, geomagnetic storms and Kp now, and the chance in the next day.
- **Mesh bot: any flight, and what's overhead.** `!flight UA123`: where that flight is now and, with a FlightAware key, its schedule (counted in the monthly limit). `OVERHEAD`: the nearest aircraft to your radio when it shares its position in its advert, else home; or `!overhead 80202`.
- **Mesh bot: news by number.** `NEWS` gives a numbered list of short headlines; send the number (or `!news 2`) for that story in 2-3 messages, `MORE` for the next ones. The AI brief now also writes a 30-character headline for each story.
- **Mesh bot: trace.** `TRACE` answers with the route your message took, by repeater name (`Alex > Lookout RPT > Golden RPT > Home Base. 2 hops, SNR 6.25 RSSI -98`). When two repeaters share an ID the name gets a "?".
- **Mesh bot: a very short AI answer.** `!ask how do I purify water with bleach` gets an answer of a line or two, telegram style, marked "AI:". Only when AI is on; admins can switch it off.
- **Mesh bot: every command on `?`.** `?` lists every command a page at a time (`? 2` for the next). Admins can switch any command off on the Family tab (WHO, PING and `?` always answer). `!where` now lists where everyone last checked in from; `!where water` still finds water.
- **Alerts on the mesh.** Each channel chooses what goes out: alerts, flights and early news (the family channel's default), only flash alerts (a trusted group's default), or nothing. Flash alerts go at any hour; the rest wait out 10 pm to 6:30 am. No alert goes twice, at most 6 an hour per channel, and reminders never go to the mesh.
- **Daily mesh brief.** At a time you pick, the family channels get the day's weather at home, alerts near us and three numbered headlines.
- ZIP codes and town names come from the Census Bureau's lists (public domain), fetched once a year into the store, so the bot finds places with no internet.
- Firearms and ammunition stay with admins: a family login can no longer add, change, move or delete them.
- Many wrong passwords from one address (20 in 15 minutes, any usernames) now make that address wait 15 minutes. Each login's own lockout still works as before.
- Pages tell browsers not to let other sites show BUNKR inside a frame, not to send BUNKR's address to links you follow out of it, and to run only BUNKR's own scripts.
- Oversized requests are refused early instead of being read into memory.
- Links from the Situation feeds (earthquakes, disasters, internet outages) are only kept if they are ordinary web addresses.
- Someone not logged in who hits an error sees "something went wrong" instead of technical details; the details go to the server log.
- The shopping list download (CSV) matches what the printed list shows for that login.
- Optional AI no longer assumes a particular AI product. To use a command-line AI tool, set its command on the hub, with a `BUNKR_AI_CLI=` line in `ai.env` or the `--ai-cli` option (any program that reads a prompt and prints a reply); the "AI (optional)" card shows it but can't change it. Or pick Ollama. A new install has no command set.
- The AI connector (MCP) works with any AI app that supports connectors: the admin lists the app's sign-in callback addresses, its web addresses and (optionally) its server networks on the same card. Only listed addresses are accepted, exactly as typed; nothing is wildcarded. A new install lists none.
- Upgrading keeps an install that used AI working as before: its command, its login file and the connector apps already signed in carry over, and the hub's older AI options still work.
- The AI tool's login file is `ai.env` next to the database; the older file name is still read when there's no `ai.env`.
- **Install with a .deb:** each release can come as `bunkr_X.Y.Z_all.deb` for Debian and Ubuntu (`sudo apt install ./bunkr_X.Y.Z_all.deb`). It puts BUNKR in `/usr/lib/bunkr`, runs it as the `bunkr` user with options in `/etc/default/bunkr`, and keeps your data in `/var/lib/bunkr` when removed (only "purge" deletes it). It won't install over a BUNKR that runs from a git checkout or was set up by the installer. See "Install with the .deb" in deploy/INSTALL.md.
- **Upgrade now:** on a .deb install, admins can upgrade from the Household tab. BUNKR itself never gets root: it leaves a request that a separate system service checks, then that service downloads the release, checks its signature and checksum, backs up the database, installs it and restarts BUNKR. If BUNKR doesn't come back, it goes back to the previous version and database. A new major version asks you to confirm twice. The card says "Upgrading…", "Upgraded to X" or "Rolled back" and why.
- Releases are signed (Ed25519). Every BUNKR carries the project's public key and refuses an upgrade whose signature doesn't check out.
- A field node with no internet can upgrade from a signed release copied into `store/bunkr-releases`.
- A hub that runs from git with auto-deploy says "This hub updates from git" instead of offering the button.
- **Offline packs** (Downloads tab, admins): make your own lists of things to keep with no internet, like car manuals, maps as PDFs, radio guides or Kiwix books. Each item is a web address (https), a book from the Kiwix catalog, or a Linux package. BUNKR fetches them every week and when you press "Fetch now", checks them (against a SHA-256 if you give one), shows size, version and when it last checked, and keeps the old copy until a new one checks out.
- Packs go to the Pi and laptop with the rest of the store. On the Nodes tab, choose which packs each node carries.
- Share a pack: "Export" saves a small file; "Import a pack" adds one someone sent you. Files are checked carefully before anything is added.
- Add your own Linux packages to the ones kept for installing with no internet (with what they need). BUNKR shows how much bigger the download gets before you add one.
- Safety: only https addresses, nothing on this machine or the home network unless you allow home-network addresses, a size limit per file (4 GB unless you change it), and downloaded files are never run.
- Offline mesh kit in the downloads store: MeshCore companion, repeater and room-server firmware for your boards (Heltec V3, Heltec T114 and RAK4631 to start; choose on Downloads), the newest release and the one before, each checked against GitHub's checksum, plus meshcore-cli and esptool to install with no internet and a README on how to flash a board. It refreshes every morning, syncs to nodes that carry firmware, and a new major version waits for your approval like other updates.
- Database copies are kept on a schedule: the newest 14 nightly copies plus the first of each month for a year, and the newest 10 copies made before a deploy. The newest copy, the one the restore test uses and anything made by hand are never removed. Admins can change the numbers on Downloads under "Space used".
- Old versions of libraries, firmware and apps are cleared out once the new version checks out; press "Keep" on one to hold on to it. Unfinished downloads left for a week go too.
- "Space used" on Downloads shows admins what the database, its copies and each part of the store take up, what the next clean-up would remove, and files the store doesn't know about (which it leaves alone).
- Admins see a "Hub computer" card on Home when security updates installed on the hub need a restart (with the packages that asked for it) or security updates are waiting, and one note under the bell when it first comes up. BUNKR never restarts anything itself.

## 1.1.0 (2026-11-22)

- The hub keeps the OS packages for label reading (Tesseract and what it needs) in the store, refreshed weekly, so field nodes and a rebuilt machine can install them with no internet. The installer uses the store's copy when one matches the system.
- News stories hold together better, without AI: two headlines need a telling word in common, not just everyday words like "kills" or "central"; headlines naming different places (Israel, Ukraine) aren't joined; each story's title is the headline most of its sources share; and a source that doesn't fit its story's title moves to the story it does fit. Recent stories are checked again once after the upgrade.
- Flagging a source moves it to the story it belongs to (or its own), re-checks the rest of that story, and says where it went. A confirmed flag teaches BUNKR to keep the two sides apart from then on; admins can see what it learned and undo it.
- AI is optional and off on a new install. One card on the Household tab, "AI (optional)", turns on AI summaries in the morning brief and news, and the AI connector (MCP), each separately. It can use a free local model (Ollama) or a command-line AI tool on the hub. Everything else works the same with AI off. An install that already used AI keeps it on.
- More barcode lookups, free and with no account: UPCitemdb (names and sizes) and USDA FoodData Central (calories for many brands). USDA also fills in calories when another source found the product without them. Each can be switched off on the Household tab ("Barcode lookups").
- Read the nutrition label: on a food item, tap "Read the nutrition label" and take a photo; the hub reads calories and servings and fills in calories each. Runs on the hub with Ubuntu's free tesseract-ocr package; nothing leaves your network.
- Barcode scans: a product Open Food Facts doesn't have now says "not found" instead of an error.
- Optional Kroger barcode lookup (Kroger, King Soopers and other Kroger stores, including store brands), set up on the Household tab.
- Scanned items are named in the household's own style (Household tab: "Names for scanned items"), by default stockroom style: "[Dry, Canned] Beans, Black, Low Sodium, Organic, Canned, 15 oz" or "[Frozen, Chicken] Breasts, Chicken, Skinless, Boneless, 2.5 lb". Sale words like "BIG DEAL!" are dropped.
  The pattern is built from pieces: pick fields and punctuation, put them in order, choose capitals and what goes between details, and see examples as you go. A few ready-made styles to start from.
- Kroger lookups fill in calories per can (from the nutrition facts), multipacks (1 pack of 4 cans) and where it's kept (Dry, Cold, Frozen).
- **Recovery tab** (Family menu), for getting back on your feet after a loss:
  - Home inventory for insurance: rooms and items (make and model, serial number, purchase date, price paid, cost to
    replace) with photos and receipts (JPEG, PNG, WebP; receipts may be PDF; up to 10 MB each). A printable list by
    room with totals and photos, and a CSV. Serial numbers and receipts are for admins only. It stays on the home hub
    (not synced to the Pi or laptop, not in the AI connector) and a full copy, photos included, goes in the encrypted
    document archive.
  - Money and accounts (admins only): bank, cards, insurance policies, retirement, loans, utilities, with who to call
    and where the paperwork is. Only the last 4 digits of a number are accepted. Printable, in the encrypted archive,
    and every view and printout is in the activity log. Never synced, never saved on phones, never in the AI connector.
  - Food plan: how many days the stored food lasts, what to eat first (soonest expiry), and a day-by-day plan for
    up to 90 days that eats the soonest-to-expire food first and mixes foods each day. Printable.
- **Kids' plan:** "Print the kids' plan" on the Family tab: one big-text page with who to call, where we meet, and what
  to do in a fire, earthquake or tornado, or when lost. No medical or private details. Also on the Pi and laptop and
  in the encrypted archive.
- **Community and comms** (Comms tab):
  - Licenses and skills per person: ham (class and callsign), GMRS, CPR, first aid, CERT and others, with expiry dates. A reminder 60 days before one expires, then weekly once expired, until it's marked done on the maintenance calendar or has a new date.
  - Neighbors: where they are, how to reach them and what they can help with (pick list plus free text). Pins on the Situation map when they have a position. Copied to field nodes; never given to the AI connector.
  - Radio nets: frequency or channel, mode, day and time, net control. The next one shows on the maintenance calendar. A check-in log of who checked in and how they were heard.
  - ICS-213 messages: write or log sent and received messages, print them in the ICS-213 layout, or copy them as plain text for Winlink Express or JS8Call.
  - Partner hubs (off until an admin sets them up): share the check-in board, the family call, the comms plan and callsigns with a neighbor's BUNKR through an address and a token shown once, read-only, logged, and revocable. Read a partner's share into a "Partners" card with its "as of" time. Never includes firearms, medical, documents, money, supplies or where anyone is.

## 1.0.0 (first public release)

- **Supplies and readiness:** inventory, locations and kits, readiness against Ready.gov targets, shopping list, barcode quick-add, scenarios.
- **Family:** check-ins with battery level, family calls and drills, medical cards and medicine supply, family plan, maintenance calendar.
- **Notifications:** phone push, the notification bell, reminder emails, document renewal reminders.
- **Situation:** alerts and hazards for your places, wildfires, earthquakes, space weather, internet outages, cyber advisories.
- **News:**
  - grouped stories, the morning brief, and flagging sources that don't belong;
  - early warning, and local watch for family towns;
  - unconfirmed public posts.
- **Flights:** following family flights.
- **Mesh radio:** MeshCore check-ins and calls over several channels, a command bot, and known nodes by key.
- **Offline kit:** encrypted documents, Kiwix libraries, maps, radio and firmware downloads; field nodes with node lock.
- **Security:** logins, passkeys, two-step login, audit log; firearms kept admin-only.
- **Version check:** a daily look for new releases, with a note to admins.
