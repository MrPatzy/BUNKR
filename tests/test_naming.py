"""Names for scanned items in a household's own style."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import naming  # noqa: E402
from bunkr.db import Store  # noqa: E402


def name(template, brand, title, **kw):
    return naming.render(template, naming.parts(brand, title, **kw))


class NamingTests(unittest.TestCase):
    def test_stockroom_style(self):
        T = naming.DEFAULT
        self.assertEqual(name(T, "Simple Truth Organic", "Simple Truth Organic® Low Sodium Black Beans BIG DEAL!", size="15 oz",
                              storage="Dry", organic=True, dept="Canned & Packaged"),
                         "[Dry, Canned] Beans, Black, Low Sodium, Organic, Canned, 15 oz")
        self.assertEqual(name(T, "StarKist", "StarKist Chunk Light Tuna in Water", size="5 oz", storage="Dry", dept="Canned & Packaged"),
                         "[Dry, Canned] Tuna, In Water, Light, Chunk, Canned, 5 oz")
        self.assertEqual(name(T, "Jif", "Jif Creamy Peanut Butter", size="16 oz", storage="Dry"), "[Dry] Peanut Butter, Creamy, 16 oz")
        self.assertEqual(name(T, "Birds Eye", "Birds Eye Frozen Sweet Corn", size="10 oz", storage="Frozen"), "[Frozen, Vegetables] Corn, Sweet, 10 oz")
        self.assertEqual(name(T, "Duracell", "Duracell Coppertop AA Batteries"), "AA Batteries, Coppertop")   # no empty brackets

    def test_frozen_and_cold_sections(self):
        T = naming.DEFAULT
        for title, want in [("Kroger Boneless Skinless Chicken Breasts", "[Frozen, Chicken] Breasts, Chicken, Skinless, Boneless, 2 lb"),
                            ("Kroger 80/20 Ground Beef Patties", "[Frozen, Beef] Patties, Beef, Ground, 80/20, 2 lb"),
                            ("Rhodes Dinner Rolls", "[Frozen, Bread] Rolls, Dinner, 2 lb"),
                            ("Marie Callender's Chicken Pot Pie", "[Frozen, Prepared] Pie, Pot, Chicken, 2 lb"),
                            ("Kroger Apple Pie", "[Frozen, Dessert] Pie, Apple, 2 lb"),
                            ("Kroger Wild Caught Salmon Fillets", "[Frozen, Seafood] Fillets, Salmon, Caught, Wild, 2 lb")]:
            brand = "Marie Callender's" if title.startswith("Marie") else title.split()[0]
            self.assertEqual(name(T, brand, title, size="2 lb", storage="Frozen"), want)
        self.assertEqual(name(T, "Kroger", "Kroger Whole Milk", size="1 gal", storage="Cold"), "[Cold, Dairy] Milk, Whole, 1 gal")
        p = naming.restock(naming.parts("Kroger", "Kroger Frozen Sweet Peas", size="12 oz", storage="Dry"), "Frozen")
        self.assertEqual(naming.render(T, p), "[Frozen, Vegetables] Peas, Sweet, 12 oz")

    def test_other_patterns(self):
        p = naming.EXAMPLE
        self.assertEqual(naming.render("", p), "Simple Truth Organic Low Sodium Black Beans")        # empty: the store's name, tidied
        self.assertEqual(naming.render("{item}, {details} ({brand}) - {size}", p),
                         "Beans, Black, Low Sodium, Organic (Simple Truth Organic) - 15 oz")
        self.assertEqual(naming.render("{brand} {item} ({size})", dict(p, brand="")), "Beans (15 oz)")

    def test_settings(self):
        with tempfile.TemporaryDirectory() as t:
            st = Store(Path(t) / "n.db")
            self.assertEqual(naming.settings(st)["template"], naming.DEFAULT)
            v = naming.save_settings(st, {"template": "{item}, {details}"})
            self.assertEqual(v["example"], "Beans, Black, Low Sodium, Organic")
            self.assertEqual(naming.save_settings(st, {"template": ""})["example"], "Simple Truth Organic Low Sodium Black Beans")
            with self.assertRaises(ValueError):
                naming.save_settings(st, {"template": "{colour}"})

    def test_builder_options(self):
        p = naming.EXAMPLE
        self.assertEqual(naming.render("{storage} | {section} | {item}: {details}", p, "upper", " / "),
                         "DRY | CANNED | BEANS: BLACK / LOW SODIUM / ORGANIC")
        self.assertEqual(naming.render("{item} - {details} - {size}", dict(p, details="")), "Beans - 15 oz")   # punctuation goes with its field
        self.assertEqual(naming.render("{item} ({brand}, {size})", dict(p, brand="", size="")), "Beans")         # empty brackets drop
        for bad in ("{item} and {size}", "[{item}", "{item})", "{item}(]"):
            with self.assertRaises(ValueError):
                naming.check({"template": bad})
        with self.assertRaises(ValueError):
            naming.check({"template": "{item}", "caps": "title"})
        self.assertEqual(len(naming.preview({"template": "{item}"})["examples"]), len(naming.EXAMPLES))
        for preset in naming.PRESETS:
            naming.check({"template": preset[1]})
        with tempfile.TemporaryDirectory() as t:
            st = Store(Path(t) / "n.db")
            naming.save_settings(st, {"template": "{item}, {details}", "caps": "lower", "joiner": " "})
            self.assertEqual(naming.name(st, p), "beans, black low sodium organic")


if __name__ == "__main__":
    unittest.main()
