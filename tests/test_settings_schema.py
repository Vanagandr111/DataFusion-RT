import unittest

from app.settings.schema import (
    DEFAULTS,
    SETTINGS_SECTIONS,
    TEST_MODE_SCOPE_LABELS,
    TEST_MODE_SCOPE_VALUES,
)


class SettingsSchemaTests(unittest.TestCase):
    def test_defaults_keep_expected_keys(self) -> None:
        self.assertEqual(DEFAULTS["furnace.driver"], "dk518")
        self.assertEqual(DEFAULTS["app.test_mode_scope"], "all")
        self.assertIn("app.theme", DEFAULTS)

    def test_settings_sections_have_known_groups(self) -> None:
        titles = [title for title, _fields in SETTINGS_SECTIONS]
        self.assertIn("Весы", titles)
        self.assertIn("Печь", titles)
        self.assertIn("Приложение", titles)

    def test_test_mode_labels_match_values(self) -> None:
        self.assertEqual(TEST_MODE_SCOPE_LABELS["all"], "Весы и печь")
        self.assertEqual(tuple(TEST_MODE_SCOPE_LABELS.values()), TEST_MODE_SCOPE_VALUES)


if __name__ == "__main__":
    unittest.main()
