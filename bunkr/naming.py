"""Names for scanned items, in a household's own style.

Store labels read like ads ("Simple Truth Organic® Low Sodium Black Beans BIG DEAL!"). Stockrooms name things the
other way round, so like items sort together: "[Dry, Canned] Beans, Black, Low Sodium, Organic, Canned, 15 oz",
"[Frozen, Chicken] Breast, Boneless Skinless, 2.5 lb".
An admin sets the pattern on the Household tab using these fields:

    {item}     the thing itself: Beans
    {details}  what kind, nearest first: Black, Low Sodium, Organic
    {form}     Canned, Frozen, Dried, Bottled, Boxed, Jarred
    {storage}  Dry, Cold or Frozen (where it's kept)
    {section}  the shelf within it: Canned, Dried (dry goods); Beef, Chicken, Seafood, Bread, Prepared,
               Vegetables, Fruit, Dessert (frozen); Dairy, Eggs and the meats (cold)
    {size}     the size of one: 15 oz
    {brand}    Simple Truth Organic
    {label}    the store's own name, tidied: Simple Truth Organic Low Sodium Black Beans

The Household tab builds the pattern from these fields and punctuation only (spaces , - / | : ; . [ ] ( )), so no
words of its own; capitals and the separator between details are options. Empty fields drop out with the punctuation
written before them, and brackets with nothing in them drop out. An empty pattern keeps the tidied store name.
"""

import re

DEFAULT = "[{storage}, {section}] {item}, {details}, {form}, {size}"
FIELDS = ("item", "details", "form", "storage", "section", "size", "brand", "label")

PROMO = re.compile(r"\b(big deal!?|value pack|club pack|bonus pack|limited edition|new!?|sale|special buy|bogo)(?=\W|$)", re.I)
MARKS = re.compile(r"[®™©]")
# two-word things that are one item ("peanut butter" is butter of a kind only to a parser)
COMPOUNDS = {"peanut butter", "almond butter", "tomato sauce", "tomato paste", "pasta sauce", "hot sauce", "soy sauce",
             "chicken broth", "beef broth", "vegetable broth", "bone broth", "olive oil", "vegetable oil", "canola oil",
             "maple syrup", "trail mix", "baking soda", "baking powder", "corn starch", "brown sugar", "powdered milk",
             "evaporated milk", "condensed milk", "coconut milk", "green beans", "refried beans", "baked beans", "pork beans",
             "mac cheese", "toilet paper", "paper towels", "hand sanitizer", "first aid kit", "drinking water", "spring water",
             "trash bags", "garbage bags", "hand warmers", "aa batteries", "aaa batteries", "d batteries", "c batteries"}
PHRASES = ["no salt added", "low sodium", "reduced sodium", "lower sodium", "less sodium", "sea salt", "fat free", "reduced fat",
           "low fat", "sugar free", "no sugar added", "whole kernel", "cream style", "extra virgin", "gluten free", "whole grain",
           "in water", "in oil", "in juice", "in syrup", "in light syrup", "in heavy syrup", "family size", "ready to eat"]
FORMS = [("Freeze-Dried", r"freeze[- ]dried"), ("Canned", r"\bcanned\b"), ("Frozen", r"\bfrozen\b"), ("Dried", r"\b(dried|dehydrated)\b"),
         ("Powdered", r"\bpowder(ed)?\b"), ("Bottled", r"\bbottled\b"), ("Jarred", r"\bjarred\b")]
# things that, from a "canned & packaged" aisle and sold by the ounce, come in a can
CANNED = re.compile(r"\b(beans?|soup|chili|corn|peas|carrots|spinach|tomatoes|tuna|salmon|sardines|chicken|spam|ham|vegetables|"
                    r"fruit|peaches|pears|pineapple|mandarin|oranges|cocktail|broth|stew|hash|milk|pumpkin|yams|potatoes|"
                    r"mushrooms|olives|chilies|beets|sauerkraut)\b", re.I)

# the shelf within frozen and cold storage; first match wins (a chicken pot pie is a prepared meal, not chicken)
SECTIONS = [
    ("Prepared", r"\b(meals?|entrees?|dinners?(?! rolls?)|pizzas?|burritos?|lasagna|pot pies?|bowls?|stir[- ]fry|enchiladas?|"
                 r"casseroles?|mac|macaroni|skillet|ravioli|tamales?|egg rolls?|potstickers|dumplings)\b"),
    ("Dessert", r"\b(ice cream|gelato|sorbet|popsicles?|frozen yogurt|cheesecake|pies?|cakes?|desserts?)\b"),
    ("Bread", r"\b(bread|toast|rolls?|buns?|bagels?|tortillas?|biscuits?|waffles?|pancakes?|croissants?|dough|crust|english muffins?)\b"),
    ("Seafood", r"\b(fish|shrimp|salmon|cod|tilapia|pollock|tuna|crab|lobster|scallops?|catfish|halibut|seafood)\b"),
    ("Chicken", r"\b(chicken|wings?|drumsticks?|thighs?)\b"),
    ("Turkey", r"\bturkey\b"),
    ("Beef", r"\b(beef|steaks?|burgers?|patties|brisket|roast|ground chuck|sirloin)\b"),
    ("Pork", r"\b(pork|bacon|ham|sausages?|chops?|ribs)\b"),
    ("Lamb", r"\b(lamb|mutton)\b"),
    ("Eggs", r"\beggs?\b"),
    ("Dairy", r"\b(milk|cheese|yogurt|butter|cream|cottage)\b"),
    ("Fruit", r"\b(berries|strawberries|blueberries|raspberries|mango|peaches|cherries|pineapple|fruit|bananas?)\b"),
    ("Vegetables", r"\b(corn|peas|broccoli|green beans|beans|carrots|spinach|vegetables?|veggies|cauliflower|potatoes|fries|"
                   r"tater tots|hash browns|okra|edamame|squash|onions|peppers)\b"),
]


def section(storage, form, text):
    """Where on the shelf: dry goods by how they're packed, frozen and cold food by what it is."""
    if storage in ("Frozen", "Cold"):
        label, item = text
        if re.search(SECTIONS[0][1], label, re.I):  # a meal first: a chicken pot pie is prepared, not chicken or pie
            return "Prepared"
        for where in (item, label):  # then the thing itself, then anything on the label
            for name, pat in SECTIONS:
                if re.search(pat, where, re.I):
                    return name
        return "Prepared" if storage == "Frozen" else ""
    return form if form not in ("Frozen", "Bottled") else ""


# what a pattern may hold besides fields: punctuation and spaces, no words
PUNCT = re.compile(r"[\[\]() ,\-/|:;.]*")
JOINERS = {", ": "comma", " ": "space", " / ": "slash", " - ": "dash", "; ": "semicolon"}
CAPS = {"": "As found", "upper": "UPPER CASE", "lower": "lower case"}
PRESETS = [("Stockroom", DEFAULT), ("Plain", "{item}, {details}, {size}"), ("Brand first", "{brand} {item}, {details} ({size})"),
           ("Shelf, then item", "{storage} | {section} | {item}, {details}, {size}"), ("Store's name", "{label}")]
HELP = {"item": "the thing: Beans", "details": "what kind: Black, Low Sodium", "form": "Canned, Dried, Bottled",
        "storage": "Dry, Cold or Frozen", "section": "the shelf: Canned, Beef, Bread, Prepared", "size": "15 oz",
        "brand": "Simple Truth Organic", "label": "the store's own name"}


def settings(store):
    s = store.get_raw("naming", {}) or {}
    return {"template": s["template"] if isinstance(s.get("template"), str) else DEFAULT,
            "caps": s.get("caps") if s.get("caps") in CAPS else "",
            "joiner": s.get("joiner") if s.get("joiner") in JOINERS else ", "}


def check(data):
    """A pattern and its options, checked: known fields, punctuation only, brackets that close."""
    t = re.sub(r"\s+", " ", str(data.get("template") or "")).strip()
    if len(t) > 200:
        raise ValueError("keep the pattern under 200 characters")
    bad = [f for f in re.findall(r"\{(\w*)\}", t) if f not in FIELDS]
    if bad:
        raise ValueError(f"unknown field {{{bad[0]}}}; use {', '.join('{' + f + '}' for f in FIELDS)}")
    if not PUNCT.fullmatch(re.sub(r"\{\w+\}", "", t)):
        raise ValueError("between fields, use only spaces and punctuation: , - / | : ; . [ ] ( )")
    stack = []
    for ch in re.sub(r"\{\w+\}", "", t):
        if ch in PAIRS:
            stack.append(PAIRS[ch])
        elif ch in ")]" and (not stack or stack.pop() != ch):
            raise ValueError(f"a {ch} doesn't close anything")
    if stack:
        raise ValueError(f"a bracket isn't closed: add {stack[-1]}")
    caps, joiner = data.get("caps") or "", data.get("joiner") or ", "
    if caps not in CAPS or joiner not in JOINERS:
        raise ValueError("pick capitals and the details separator from the lists")
    return {"template": t, "caps": caps, "joiner": joiner}


def save_settings(store, data):
    store.set_raw("naming", check(data))
    return view(store)


def preview(data):
    s = check(data)
    return {"examples": [render(s["template"], p, s["caps"], s["joiner"]) for p in EXAMPLES]}


def view(store):
    s = settings(store)
    return s | {"default": DEFAULT, "fields": list(FIELDS), "help": HELP, "presets": [{"name": n, "template": t} for n, t in PRESETS],
                "caps_options": CAPS, "joiners": JOINERS, "example": render(s["template"], EXAMPLE, s["caps"], s["joiner"]),
                "examples": [render(s["template"], p, s["caps"], s["joiner"]) for p in EXAMPLES]}


def tidy(text):
    """Drop trademark signs and sale words."""
    t = PROMO.sub("", MARKS.sub("", str(text or "")))
    return re.sub(r"\s+", " ", t).strip(" -,!")


def _title(w):
    return " ".join(x if x.isupper() and len(x) <= 4 else x[:1].upper() + x[1:].lower() for x in w.split())


def size_text(amount, unit):
    if not amount:
        return ""
    n = f"{float(amount):g}"
    return f"{n} {unit}".strip()


def parts(brand, title, *, size="", storage="", form="", organic=False, dept=""):
    """Split a store label into naming fields."""
    brand = tidy(brand)
    label = tidy(title)
    if brand and not label.lower().startswith(brand.lower()):
        label = f"{brand} {label}"
    rest = label[len(brand):].strip() if brand and label.lower().startswith(brand.lower()) else label
    # a brand's own words at the front ("Kroger", "Simple Truth Organic")
    for w in brand.split():
        if rest.lower().startswith(w.lower() + " "):
            rest = rest[len(w):].strip()
    low = rest.lower()
    for name, pat in FORMS:
        if re.search(pat, low):
            form = form or name
            rest = re.sub(pat, " ", rest, flags=re.I)
    tail = ""
    m = re.search(r"\s(in|with)\s(.+)$", rest, re.I)
    if m:
        tail, rest = f"{m.group(1).lower()} {m.group(2)}", rest[:m.start()]
    words = [w for w in re.split(r"[\s,]+", rest.strip()) if w and w not in ("&", "-", "and")]
    item = ""
    if len(words) >= 2 and " ".join(words[-2:]).lower() in COMPOUNDS:
        item, words = " ".join(words[-2:]), words[:-2]
    elif words:
        item, words = words[-1], words[:-1]
    text = " ".join(words)
    # phrases first, then single words; nearest the item first
    found = []
    for p in sorted(PHRASES, key=len, reverse=True):
        for m2 in re.finditer(r"\b" + re.escape(p) + r"\b", text, re.I):
            found.append((m2.start(), m2.group(0)))
            text = text[:m2.start()] + "#" * len(m2.group(0)) + text[m2.end():]
    for m2 in re.finditer(r"[^\s#]+", text):
        found.append((m2.start(), m2.group(0)))
    details = [_title(w) for _, w in sorted(found, key=lambda x: -x[0])]
    if tail:
        details.insert(0, _title(tail))
    if organic and not any(d.lower() == "organic" for d in details):
        details.append("Organic")
    if not form and re.search(r"canned", dept or "", re.I) and re.search(r"\boz\b", size or "") and CANNED.search(item + " " + " ".join(details)):
        form = "Canned"
    return {"item": _title(item), "details": ", ".join(details), "form": "" if form == storage else form, "storage": storage,
            "section": section(storage, form, (label, item)), "size": size,
            "brand": brand, "label": label, "_form": form}


def restock(p, storage):
    """Move parts to another storage (Dry/Cold/Frozen, or "" for non-food) and redo the section."""
    form = p.get("_form", p.get("form", ""))
    p.update(storage=storage, form="" if form == storage else form, section=section(storage, form, (p["label"], p["item"])))
    return p


TOKEN = re.compile(r"\{(\w+)\}|([\[(])|([\])])|([^{}\[\]()]+)")
PAIRS = {"[": "]", "(": ")"}


def _parse(template):
    """A pattern -> fields, punctuation and bracket groups."""
    root = []
    stack = [root]
    for m in TOKEN.finditer(template):
        f, o, c, s = m.groups()
        if f:
            stack[-1].append(("f", f))
        elif o:
            g = ("g", o, [])
            stack[-1].append(g)
            stack.append(g[2])
        elif c:
            if len(stack) > 1:
                stack.pop()
        elif s:
            stack[-1].append(("s", s))
    return root


def _seq(items, p, joiner, group):
    """Join the parts that have a value, each with the punctuation written before it; empty parts drop out with theirs."""
    out, gap, seen = "", "", set()
    for it in items:
        if it[0] == "s":
            gap += it[1]
            continue
        if it[0] == "f":
            v = re.sub(r"\s+", " ", str(p.get(it[1]) or "")).strip()
            if it[1] == "details" and joiner != ", ":
                v = v.replace(", ", joiner)
            if group and v.lower() in seen:  # [Frozen, Frozen] -> [Frozen]
                v = ""
            seen.add(v.lower())
        else:
            inner = _seq(it[2], p, joiner, True)
            v = it[1] + inner + PAIRS[it[1]] if inner else ""
        if v:
            out += (gap if out else "") + v
        gap = ""
    return out


def render(template, p, caps="", joiner=", "):
    if not template:
        return p.get("label") or ""
    out = re.sub(r"\s+", " ", _seq(_parse(template), p, joiner, False)).strip(" ,-/|:;")
    out = out.upper() if caps == "upper" else out.lower() if caps == "lower" else out
    return out[:120] or p.get("label") or ""


def name(store, p):
    s = settings(store)
    return render(s["template"], p, s["caps"], s["joiner"])


EXAMPLE = parts("Simple Truth Organic", "Simple Truth Organic® Low Sodium Black Beans BIG DEAL!", size="15 oz", storage="Dry",
                organic=True, dept="Canned & Packaged")
EXAMPLES = [EXAMPLE, parts("Kroger", "Kroger Boneless Skinless Chicken Breasts", size="2.5 lb", storage="Frozen"),
            parts("Kroger", "Kroger Whole Milk", size="1 gal", storage="Cold"), parts("Duracell", "Duracell Coppertop AA Batteries")]
