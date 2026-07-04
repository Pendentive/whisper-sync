"""docs/features/ must stay in sync with the shipped configuration.

The features folder is the source the installer and README draw from,
so drift is a real user-facing bug: a setting without documentation,
or documentation for a setting that no longer exists. These tests make
the mechanical parts of the same-PR docs rule enforceable - key
inventory and default hotkeys - so only the prose is on the author.
"""

import json
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).parent.parent
_DEFAULTS = json.loads(
    (_REPO / "whisper_sync" / "config.defaults.json").read_text(encoding="utf-8"))
_FEATURES_DIR = _REPO / "docs" / "features"


class DefaultsDocTests(unittest.TestCase):
    def setUp(self):
        self.doc = (_FEATURES_DIR / "defaults.md").read_text(encoding="utf-8")
        # Keys documented as `key` at the start of a table row.
        self.doc_keys = set(re.findall(r"^\| `([a-z_0-9]+)`", self.doc,
                                       flags=re.MULTILINE))

    def test_every_config_key_is_documented(self):
        missing = set(_DEFAULTS) - self.doc_keys
        self.assertFalse(
            missing,
            f"config.defaults.json keys missing from docs/features/defaults.md: "
            f"{sorted(missing)} - document them in the same PR",
        )

    def test_no_stale_keys_are_documented(self):
        stale = self.doc_keys - set(_DEFAULTS)
        self.assertFalse(
            stale,
            f"docs/features/defaults.md documents keys that no longer exist: "
            f"{sorted(stale)} - remove them in the same PR",
        )

    def test_documented_defaults_match_for_scalar_keys(self):
        # Spot-check the values users most depend on.
        for key in ("auto_sleep_minutes", "model", "paste_method",
                    "dictation_max_minutes", "gpu_guard_low_vram_mb"):
            row = re.search(rf"^\| `{key}` \| `([^`]+)`", self.doc,
                            flags=re.MULTILINE)
            self.assertIsNotNone(row, f"no default cell for {key}")
            self.assertEqual(row.group(1), str(_DEFAULTS[key]),
                             f"documented default for {key} is stale")


class ShortcutsDocTests(unittest.TestCase):
    def test_default_hotkeys_are_documented_verbatim(self):
        doc = (_FEATURES_DIR / "shortcuts.md").read_text(encoding="utf-8")
        for name, combo in _DEFAULTS["hotkeys"].items():
            self.assertIn(combo, doc,
                          f"default hotkey {name}={combo} missing from "
                          "docs/features/shortcuts.md")


class FeaturesDocTests(unittest.TestCase):
    def test_features_doc_exists_and_links_companions(self):
        doc = (_FEATURES_DIR / "features.md").read_text(encoding="utf-8")
        self.assertIn("shortcuts.md", doc)
        self.assertIn("defaults.md", doc)


if __name__ == "__main__":
    unittest.main()
