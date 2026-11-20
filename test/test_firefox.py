import unittest
from unittest.mock import Mock
from xml.etree import ElementTree

from puma.apps.android.firefox.firefox import FIREFOX_PACKAGE, Firefox, _read_history_rows
from puma.apps.android.firefox import xpaths as xp
from puma.state_graph.state import _shortest_path
from puma.state_graph.state_graph import StateGraph


class TestFirefox(unittest.TestCase):
    def setUp(self):
        self.app = Firefox.__new__(Firefox)
        self.app.driver = Mock()
        self.app.gtl_logger = Mock()
        self.app.go_to_state = Mock()

    def test_version_and_graph(self):
        self.assertEqual(FIREFOX_PACKAGE, 'org.mozilla.firefox')
        self.assertEqual(Firefox.supported_version, '160.0.1')
        self.assertTrue(issubclass(Firefox, StateGraph))
        initial_states = [state for state in Firefox.states if state.initial_state]
        self.assertEqual(initial_states, [Firefox.browser_state])
        for state in Firefox.states:
            with self.subTest(state=state):
                self.assertIsNotNone(_shortest_path(state, Firefox.browser_state))
                if not state.recognize_only:
                    self.assertIsNotNone(_shortest_path(Firefox.browser_state, state))

    def test_navigation_rejects_non_http_urls(self):
        for url in ('example.org', 'https:///missing-host', 'file:///sdcard/test.pdf'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Firefox._check_url(url)

    def test_url_xpath_quotes_mixed_punctuation(self):
        value = """https://example.org/"two"/one's"""
        self.assertEqual(xp.literal(value), """concat('https://example.org/"two"/one', "'", 's')""")

    def test_history_reads_visible_rows_and_skips_unrelated_titles(self):
        root = ElementTree.fromstring(
            '<hierarchy>'
            '<node resource-id="org.mozilla.firefox:id/title" text="Unrelated title"/>'
            '<node resource-id="org.mozilla.firefox:id/history_layout">'
            '  <node resource-id="org.mozilla.firefox:id/title" text="Synthetic page"/>'
            '  <node resource-id="org.mozilla.firefox:id/url" text="https://example.org/01"/>'
            '</node>'
            '<node resource-id="org.mozilla.firefox:id/history_layout"/>'
            '</hierarchy>'
        )
        self.assertEqual(_read_history_rows(root),
                         [{'title': 'Synthetic page', 'url': 'https://example.org/01'}])

    def test_close_tab_refuses_ambiguous_title(self):
        first, second = Mock(), Mock()
        self.app.driver.is_present.return_value = True
        self.app.driver.get_elements.return_value = [first, second]
        self.assertFalse(self.app.close_tab('Repeated title'))
        first.click.assert_not_called()
        second.click.assert_not_called()

    def test_close_tab_uses_requested_mode_and_exact_title(self):
        element = Mock()
        self.app.driver.is_present.side_effect = lambda locator: locator != xp.TABS_ADD_PRIVATE
        self.app.driver.get_elements.return_value = [element]
        self.assertTrue(self.app.close_tab('Synthetic tab', private=True))
        self.app.driver.click.assert_called_once_with(xp.TABS_SELECT_PRIVATE)
        self.app.driver.get_elements.assert_called_once_with(
            "//android.widget.Button[@content-desc='Close tab Synthetic tab']",
        )
        element.click.assert_called_once_with()

    def test_delete_bookmark_refuses_duplicate_url(self):
        self.app.driver.is_present.return_value = True
        self.app.driver.get_elements.return_value = [Mock(), Mock()]
        self.assertFalse(self.app.delete_bookmark('https://example.org/01'))
        self.app.driver.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
