# Changelog

BUNKR uses version numbers MAJOR.MINOR.PATCH. The hub tells admins when a newer release is out; upgrading is
always your choice (see "Upgrading" in deploy/INSTALL.md).

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
