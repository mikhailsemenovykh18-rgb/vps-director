"""Translations: every language has the same placeholders as English, so a message never breaks."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vps_director import texts  # noqa: E402

FIELD = re.compile(r"(?<!\{)\{[^{}]*\}(?!\})")


def fields(value):
    return sorted(FIELD.findall(value)) if isinstance(value, str) else None


class BotTextsTest(unittest.TestCase):
    def test_every_language_has_every_key_with_the_same_placeholders(self):
        en = texts.TEXTS["en"]
        for lang in ("ru",) + texts.EXTRA_LANGS:
            other = texts.TEXTS[lang]
            for key, value in en.items():
                self.assertIn(key, other, (lang, key))
                if key == "cmds":
                    self.assertEqual(set(value), set(other[key]), lang)
                    continue
                self.assertEqual(fields(value), fields(other[key]), (lang, key, other[key]))

    def test_translations_exist(self):
        for lang in texts.EXTRA_LANGS:
            from importlib import import_module
            mod = import_module("vps_director.lang." + lang)
            self.assertGreater(len(mod.TEXTS), 300, lang)  # translated, not just falling back

    def test_srv_cores_has_plural_forms(self):
        for lang in texts.EXTRA_LANGS:
            self.assertEqual(len(texts.TEXTS[lang]["srv_cores"].split("|")), 2, lang)
            self.assertEqual(len(texts.TEXTS[lang]["weekdays"].split()), 7, lang)


if __name__ == "__main__":
    unittest.main()
