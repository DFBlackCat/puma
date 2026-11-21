from contextlib import redirect_stderr
from hashlib import sha256
import io
import logging
from pathlib import Path
import re
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen
from xml.etree import ElementTree

import elementpath

from puma.apps.android.firefox.firefox import (
    FIREFOX_PACKAGE,
    Firefox,
    FirefoxAmbiguousActionError,
    FirefoxNavigationError,
    FirefoxPreconditionError,
    _address_matches_url,
    _enter_normal_browser,
    _enter_private_browser,
    _parse_site_info,
    _read_history_rows,
    _return_to_browser,
)
from puma.apps.android.firefox import xpaths as xp
from puma.state_graph.puma_driver import PumaClickException
from puma.state_graph.state import _shortest_path
from puma.state_graph.state_graph import StateGraph
from test_scripts import test_firefox as firefox_runner


def _make_fake_wait_until(max_polls: int = 4):
    def fake_wait_until(condition, timeout=5, interval=0.5, description=None):
        for _ in range(max_polls):
            if condition():
                return
        raise TimeoutError(f'Timed out waiting for {description}')

    return fake_wait_until


class _RecordingLogger:
    """Capture structured GTL messages with their log levels for offline assertions."""

    def __init__(self) -> None:
        self.entries: list[tuple[int, str]] = []

    def info(self, msg: str, *args) -> None:
        self.entries.append((logging.INFO, msg % args if args else msg))

    def warning(self, msg: str, *args) -> None:
        self.entries.append((logging.WARNING, msg % args if args else msg))

    def warn(self, msg: str, *args) -> None:
        self.warning(msg, *args)

    def error(self, msg: str, *args) -> None:
        self.entries.append((logging.ERROR, msg % args if args else msg))

    def exception(self, msg: str, *args) -> None:
        self.entries.append((logging.ERROR, msg % args if args else msg))

    @property
    def messages(self) -> list[str]:
        return [message for _, message in self.entries]


class TestFirefox(unittest.TestCase):
    def setUp(self):
        self.app = Firefox.__new__(Firefox)
        self.app.driver = Mock()
        self.app.gtl_logger = Mock()
        self.app.go_to_state = Mock()
        self.app.current_state = Firefox.browser_state
        self.app.try_restart = True
        self.app.app_popups = []

    # ------------------------------------------------------------------
    # Existing baseline tests (preserved)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # P0: Read-only operations must not mutate browser evidence
    # ------------------------------------------------------------------

    def test_has_collection_reads_visible_home_without_creating_tab(self):
        self.app.gtl_logger = _RecordingLogger()
        target = xp.collection_name('Forensic Collection')
        self.app.driver.is_present.side_effect = lambda locator: locator in (xp.BROWSER_HOME, target)

        self.assertTrue(self.app.has_collection('Forensic Collection'))
        self.app.driver.click.assert_not_called()
        self.assertEqual(Firefox.has_collection.action_state, Firefox.browser_state)

    def test_has_collection_refuses_to_create_tab_when_home_not_visible(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.driver.is_present.return_value = False
        self.app.recover_state = Mock()

        with self.assertRaises(FirefoxPreconditionError):
            self.app.has_collection('Forensic Collection')

        self.app.driver.click.assert_not_called()
        self.assertTrue(any('stage=INCONCLUSIVE' in msg for msg in self.app.gtl_logger.messages))

    def test_return_to_browser_from_tabs_selects_existing_normal_tab_without_mutating(self):
        driver = Mock()
        driver.gtl_logger = _RecordingLogger()
        state = {'selected': False}

        def is_present(locator: str) -> bool:
            if locator == xp.TABS_MENU:
                return not state['selected']
            if locator == xp.TABS_ADD_NORMAL:
                return True
            if locator == xp.TABS_EXISTING_CARD:
                return True
            if locator == xp.BROWSER_NORMAL:
                return state['selected']
            return False

        def click(locator: str) -> None:
            if locator == xp.TABS_EXISTING_CARD:
                state['selected'] = True

        driver.is_present.side_effect = is_present
        driver.click.side_effect = click
        driver.wait_until.side_effect = _make_fake_wait_until()

        _return_to_browser(driver)

        clicked = [call.args[0] for call in driver.click.call_args_list]
        self.assertIn(xp.TABS_EXISTING_CARD, clicked)
        self.assertNotIn(xp.TABS_ADD_NORMAL, clicked)
        self.assertNotIn(xp.TABS_ADD_PRIVATE, clicked)

    def test_return_to_browser_from_private_mode_selects_existing_normal_tab(self):
        driver = Mock()
        driver.gtl_logger = _RecordingLogger()
        phase = {'step': 'private'}

        def is_present(locator: str) -> bool:
            if phase['step'] == 'private':
                return locator in (xp.BROWSER_ADDRESS, xp.BROWSER_PRIVATE)
            if phase['step'] == 'tabs':
                return locator in (xp.TABS_MENU, xp.TABS_ADD_NORMAL, xp.TABS_EXISTING_CARD)
            return locator in (xp.BROWSER_ADDRESS, xp.BROWSER_NORMAL)

        def click(locator: str) -> None:
            if locator == xp.BROWSER_TAB_COUNTER:
                phase['step'] = 'tabs'
            elif locator == xp.TABS_EXISTING_CARD:
                phase['step'] = 'normal'

        driver.is_present.side_effect = is_present
        driver.click.side_effect = click
        driver.wait_until.side_effect = _make_fake_wait_until()

        _return_to_browser(driver)

        clicked = [call.args[0] for call in driver.click.call_args_list]
        self.assertEqual(clicked, [xp.BROWSER_TAB_COUNTER, xp.TABS_EXISTING_CARD])
        self.assertNotIn(xp.TABS_ADD_NORMAL, clicked)

    def test_return_to_browser_logs_unavoidable_mutation_only_when_no_normal_tab_exists(self):
        driver = Mock()
        driver.gtl_logger = _RecordingLogger()
        opened = {'blank': False}

        def is_present(locator: str) -> bool:
            if locator == xp.TABS_MENU:
                return not opened['blank']
            if locator == xp.TABS_ADD_NORMAL:
                return True
            if locator == xp.TABS_EXISTING_CARD:
                return False
            if locator == xp.BROWSER_NORMAL:
                return opened['blank']
            return False

        def click(locator: str) -> None:
            if locator == xp.TABS_ADD_NORMAL:
                opened['blank'] = True

        driver.is_present.side_effect = is_present
        driver.click.side_effect = click
        driver.wait_until.side_effect = _make_fake_wait_until()

        _return_to_browser(driver)

        clicked = [call.args[0] for call in driver.click.call_args_list]
        self.assertEqual(clicked, [xp.TABS_ADD_NORMAL])
        self.assertTrue(
            any(level == logging.WARNING and 'stage=STATE_RECOVERY_MUTATION' in msg
                for level, msg in driver.gtl_logger.entries)
        )

    def test_tabs_to_browser_transitions_reuse_existing_tabs(self):
        for transition_fn, add_locator, mode_locator in (
            (_enter_normal_browser, xp.TABS_ADD_NORMAL, xp.BROWSER_NORMAL),
            (_enter_private_browser, xp.TABS_ADD_PRIVATE, xp.BROWSER_PRIVATE),
        ):
            with self.subTest(transition=transition_fn.__name__):
                driver = Mock()
                driver.gtl_logger = _RecordingLogger()
                selected = {'done': False}

                def is_present(locator: str, add_loc=add_locator, mode_loc=mode_locator, sel=selected) -> bool:
                    if locator == add_loc:
                        return True
                    if locator == xp.TABS_EXISTING_CARD:
                        return True
                    if locator == mode_loc:
                        return sel['done']
                    return False

                def click(locator: str, sel=selected) -> None:
                    if locator == xp.TABS_EXISTING_CARD:
                        sel['done'] = True

                driver.is_present.side_effect = is_present
                driver.click.side_effect = click
                driver.wait_until.side_effect = _make_fake_wait_until()

                transition_fn(driver)
                clicked = [call.args[0] for call in driver.click.call_args_list]
                self.assertEqual(clicked, [xp.TABS_EXISTING_CARD])
                self.assertNotIn(add_locator, clicked)

    def test_read_only_observations_do_not_create_tabs_or_bookmarks(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.driver.is_present.return_value = True
        self.app.driver.wait_until_present.return_value = True
        self.app.driver.wait_until.side_effect = _make_fake_wait_until()
        self.app.driver.driver.page_source = (
            '<hierarchy>'
            '<android.widget.Button content-desc="Close tab Synthetic Page"/>'
            '<node resource-id="org.mozilla.firefox:id/history_layout">'
            '  <node resource-id="org.mozilla.firefox:id/title" text="Synthetic Page"/>'
            '  <node resource-id="org.mozilla.firefox:id/url" text="https://example.org/01"/>'
            '</node>'
            '<node resource-id="unified.trust.panel.website.url" text="example.org"/>'
            '<node><node text="Enhanced Tracking Protection"/><node text="On"/></node>'
            '</hierarchy>'
        )

        self.assertEqual(self.app.inspect_tabs(), ['Synthetic Page'])
        self.assertEqual(len(self.app.inspect_history()), 1)
        self.assertEqual(len(self.app.inspect_recently_closed()), 1)
        self.assertTrue(self.app.has_bookmark('https://example.org/01'))
        self.assertTrue(self.app.has_download('sample.pdf'))
        self.assertEqual(self.app.inspect_site_info(), {'url': 'example.org', 'tracking_protection': 'On'})

        self.app.driver.driver.page_source = (
            '<hierarchy>'
            '  <node class="android.widget.LinearLayout">'
            '    <node resource-id="android:id/title" text="Search"/>'
            '    <node resource-id="android:id/summary" text="Google"/>'
            '  </node>'
            '</hierarchy>'
        )
        self.assertEqual(self.app.inspect_settings(), [{'title': 'Search', 'summary': 'Google'}])

        clicked = [call.args[0] for call in self.app.driver.click.call_args_list]
        for forbidden in (xp.TABS_ADD_NORMAL, xp.TABS_ADD_PRIVATE, xp.MENU_ADD_BOOKMARK, xp.BOOKMARKS_MENU_DELETE):
            self.assertNotIn(forbidden, clicked)

    def test_real_state_graph_navigation_from_tabs_to_history_avoids_creating_tab(self):
        app = Firefox.__new__(Firefox)
        app.driver = Mock()
        app.gtl_logger = _RecordingLogger()
        app.driver.gtl_logger = app.gtl_logger
        app.current_state = Firefox.tabs_state
        app.try_restart = True
        app.app_popups = []

        screen = {'name': 'tabs'}

        def is_present(locator: str) -> bool:
            mapping = {
                'tabs': {xp.TABS_MENU, xp.TABS_ADD_NORMAL, xp.TABS_EXISTING_CARD},
                'browser': {xp.BROWSER_NORMAL, xp.BROWSER_ADDRESS},
                'menu': {xp.MENU_CLOSE, xp.MENU_HISTORY},
                'history': {xp.HISTORY_LAYOUT},
            }
            return locator in mapping[screen['name']]

        def click(locator: str) -> None:
            if locator == xp.TABS_EXISTING_CARD:
                screen['name'] = 'browser'
            elif locator == xp.BROWSER_MENU:
                screen['name'] = 'menu'

        def swipe_to_find(locator: str, max_swipes: int = 10, swipe_down: bool = True):
            elem = Mock()
            if locator == xp.MENU_HISTORY:
                elem.click.side_effect = lambda: screen.__setitem__('name', 'history')
            return elem

        app.driver.is_present.side_effect = is_present
        app.driver.click.side_effect = click
        app.driver.swipe_to_find_element.side_effect = swipe_to_find
        app.driver.wait_until.side_effect = _make_fake_wait_until()
        app.driver.driver.page_source = (
            '<hierarchy>'
            '<node resource-id="org.mozilla.firefox:id/history_layout">'
            '  <node resource-id="org.mozilla.firefox:id/title" text="Visited"/>'
            '  <node resource-id="org.mozilla.firefox:id/url" text="https://example.org/v"/>'
            '</node>'
            '</hierarchy>'
        )

        rows = app.inspect_history()
        self.assertEqual(rows, [{'title': 'Visited', 'url': 'https://example.org/v'}])
        self.assertIs(app.current_state, Firefox.history_state)
        clicked = [call.args[0] for call in app.driver.click.call_args_list]
        self.assertIn(xp.TABS_EXISTING_CARD, clicked)
        self.assertNotIn(xp.TABS_ADD_NORMAL, clicked)
        self.assertNotIn(xp.TABS_ADD_PRIVATE, clicked)

    # ------------------------------------------------------------------
    # P0: Navigation verification & stale WebView protection
    # ------------------------------------------------------------------

    def test_address_matches_url_handles_schemes_ports_paths_and_queries(self):
        self.assertTrue(_address_matches_url('127.0.0.1:8080/run/01-visit', 'http://127.0.0.1:8080/run/01-visit'))
        self.assertTrue(_address_matches_url('https://example.org/', 'https://example.org'))
        self.assertTrue(_address_matches_url('www.example.org', 'https://example.org', strict=False))
        self.assertFalse(_address_matches_url('www.example.org', 'https://example.org', strict=True))
        self.assertFalse(_address_matches_url('127.0.0.1:8080/run/01-visit', 'http://127.0.0.1:8080/run/02-other'))
        self.assertFalse(_address_matches_url('127.0.0.1:9999/run/01-visit', 'http://127.0.0.1:8080/run/01-visit'))
        self.assertFalse(_address_matches_url('example.org/search?q=old', 'https://example.org/search?q=new'))
        self.assertTrue(_address_matches_url('example.org/search?q=new', 'https://example.org/search?q=new'))
        self.assertTrue(_address_matches_url('example.org/search?q=hello%20world', 'https://example.org/search?q=hello+world'))
        self.assertFalse(_address_matches_url('Search or enter address', 'https://example.org'))
        self.assertFalse(_address_matches_url('', 'https://example.org'))

    def _configure_navigation_driver(
        self,
        *,
        initial_address: str,
        initial_title: str,
        polls: list[dict],
    ) -> dict:
        """Build a stateful fake driver simulating address editor and post-Enter polling frames."""
        state = {
            'editing': False,
            'edit_text': '',
            'entered': False,
            'poll_index': -1,
            'press_enter_calls': 0,
            'current_address': initial_address,
            'current_title': initial_title,
            'current_text': '',
            'error_page': False,
            'webview_present': True,
        }

        def advance_poll() -> None:
            if state['entered'] and polls:
                state['poll_index'] = min(state['poll_index'] + 1, len(polls) - 1)
                frame = polls[state['poll_index']]
                state['editing'] = frame.get('editing', False)
                state['current_address'] = frame.get('address', state['current_address'])
                state['current_title'] = frame.get('title', state['current_title'])
                state['current_text'] = frame.get('text', state['current_text'])
                state['error_page'] = frame.get('error', False)
                state['webview_present'] = frame.get('webview', True)

        def is_present(locator: str) -> bool:
            if locator == xp.BROWSER_EDIT:
                return state['editing']
            if locator == xp.BROWSER_ADDRESS:
                return not state['editing']
            if locator == xp.BROWSER_VIEW:
                return state['webview_present']
            if locator == xp.BROWSER_ERROR:
                return state['error_page']
            if locator == xp.POPUP_NOTIFICATIONS:
                return False
            if locator.startswith(f'{xp.BROWSER_VIEW}[@text='):
                return state['webview_present'] and locator == xp.browser_view_with_title(state['current_title'])
            if locator.startswith(f'{xp.BROWSER_VIEW}//*[contains(@text, '):
                return state['webview_present'] and bool(state['current_text']) and state['current_text'] in locator
            if locator in (xp.BROWSER_NORMAL, xp.BROWSER_PRIVATE):
                return True
            return False

        def get_element(locator: str):
            elem = Mock()
            if locator == xp.BROWSER_EDIT:
                elem.get_attribute.side_effect = lambda attr: state['edit_text'] if attr == 'text' else ''
            elif locator == xp.BROWSER_ADDRESS:
                elem.get_attribute.side_effect = (
                    lambda attr: f"{state['current_address']}. Search or enter address"
                    if attr == 'content-desc' else ''
                )
            elif locator == xp.BROWSER_VIEW:
                elem.get_attribute.side_effect = lambda attr: state['current_title'] if attr == 'text' else ''
            return elem

        def click(locator: str) -> None:
            if locator == xp.BROWSER_ADDRESS:
                state['editing'] = True

        def send_keys(locator: str, text: str) -> None:
            if locator == xp.BROWSER_EDIT:
                state['edit_text'] = text

        def press_enter() -> None:
            state['press_enter_calls'] += 1
            state['entered'] = True
            state['editing'] = False

        def wait_until(condition, timeout=5, interval=0.5, description=None):
            if description == 'Firefox destination navigation':
                for _ in range(max(len(polls), 1)):
                    advance_poll()
                    if condition():
                        return
                raise TimeoutError(f'Timed out waiting for {description}')
            if condition():
                return
            raise TimeoutError(f'Timed out waiting for {description}')

        self.app.driver.is_present.side_effect = is_present
        self.app.driver.get_element.side_effect = get_element
        self.app.driver.click.side_effect = click
        self.app.driver.send_keys.side_effect = send_keys
        self.app.driver.press_enter.side_effect = press_enter
        self.app.driver.wait_until.side_effect = wait_until
        return state

    def test_visit_url_rejects_stale_webview_until_new_destination_loads(self):
        self.app.gtl_logger = _RecordingLogger()
        state = self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/run/01-old',
            initial_title='Old Page',
            polls=[
                # Poll 0: editor closed, but stale previous page is still rendered
                {'address': '127.0.0.1:8080/run/01-old', 'title': 'Old Page'},
                # Poll 1: address updated, but page title is still stale
                {'address': '127.0.0.1:8080/run/02-new', 'title': 'Old Page'},
                # Poll 2: destination URL, title, and body text are all present
                {'address': '127.0.0.1:8080/run/02-new', 'title': 'New Page', 'text': 'Loaded body'},
            ],
        )

        observed = self.app.visit_url(
            'http://127.0.0.1:8080/run/02-new',
            expected_title='New Page',
            expected_text='Loaded body',
        )

        self.assertEqual(observed, '127.0.0.1:8080/run/02-new')
        self.assertEqual(state['poll_index'], 2)
        self.assertEqual(state['press_enter_calls'], 1)
        self.assertTrue(any('stage=VERIFIED' in msg for msg in self.app.gtl_logger.messages))

    def test_visit_url_times_out_when_only_stale_page_remains(self):
        self.app.gtl_logger = _RecordingLogger()
        state = self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/run/01-old',
            initial_title='Old Page',
            polls=[
                {'address': '127.0.0.1:8080/run/01-old', 'title': 'Old Page'},
                {'address': '127.0.0.1:8080/run/01-old', 'title': 'Old Page'},
            ],
        )

        with self.assertRaises(FirefoxNavigationError) as ctx:
            self.app.visit_url('http://127.0.0.1:8080/run/02-new', timeout=1)

        self.assertIn('Timed out waiting for Firefox navigation', str(ctx.exception))
        # Commit-point retry guard must suppress a second press_enter submission.
        self.assertEqual(state['press_enter_calls'], 1)
        self.assertTrue(any('stage=FAILED' in msg for msg in self.app.gtl_logger.messages))
        self.assertTrue(any('stage=RETRY_SUPPRESSED' in msg for msg in self.app.gtl_logger.messages))

    def test_visit_url_detects_browser_error_page_immediately(self):
        self.app.gtl_logger = _RecordingLogger()
        state = self._configure_navigation_driver(
            initial_address='example.org',
            initial_title='Home',
            polls=[
                {'address': 'unreachable.invalid', 'error': True, 'webview': False},
                {'address': 'unreachable.invalid', 'error': False, 'webview': True},
            ],
        )

        with self.assertRaises(FirefoxNavigationError) as ctx:
            self.app.visit_url('https://unreachable.invalid')

        self.assertIn('browser error page', str(ctx.exception))
        self.assertEqual(state['poll_index'], 0)
        self.assertEqual(state['press_enter_calls'], 1)

    def test_visit_url_private_enforces_expected_url_after_redirect(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.current_state = Firefox.private_browser_state
        self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/start',
            initial_title='Start',
            polls=[
                {'address': '127.0.0.1:8080/redirected', 'title': 'Final Private Page'},
            ],
        )

        observed = self.app.visit_url_private(
            'http://127.0.0.1:8080/start',
            expected_url='http://127.0.0.1:8080/redirected',
            expected_title='Final Private Page',
        )
        self.assertEqual(observed, '127.0.0.1:8080/redirected')

    def test_search_rejects_blank_query_and_stale_unchanged_page(self):
        with self.assertRaises(ValueError):
            self.app.search('   ')

        self.app.gtl_logger = _RecordingLogger()
        state = self._configure_navigation_driver(
            initial_address='example.org/previous',
            initial_title='Previous Page',
            polls=[
                {'address': 'example.org/previous', 'title': 'Previous Page'},
            ],
        )
        with self.assertRaises(FirefoxNavigationError):
            self.app.search('PUMA synthetic query', timeout=1)
        self.assertEqual(state['press_enter_calls'], 1)

    # ------------------------------------------------------------------
    # P1: Non-idempotent action and retry safety
    # ------------------------------------------------------------------

    def test_save_to_new_collection_suppresses_duplicate_on_retry_after_done(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.driver.is_present.side_effect = lambda locator: locator in (
            xp.BROWSER_NORMAL, xp.BROWSER_ADDRESS, xp.BROWSER_VIEW, xp.COLLECTION_PICKER, xp.COLLECTION_NAME,
        )
        name_elem = Mock()
        name_elem.get_attribute.return_value = 'Unique Collection'
        self.app.driver.get_element.return_value = name_elem
        self.app.driver.wait_until.side_effect = _make_fake_wait_until()
        self.app.driver.wait_until_present.side_effect = RuntimeError('Transient driver drop after Done')

        with self.assertRaises(FirefoxAmbiguousActionError):
            self.app.save_to_new_collection('Unique Collection')

        self.app.driver.driver.execute_script.assert_called_once_with(
            'mobile: performEditorAction', {'action': 'done'},
        )
        self.assertTrue(any('stage=RETRY_SUPPRESSED' in msg for msg in self.app.gtl_logger.messages))

    def test_save_to_new_collection_logs_ambiguous_when_toast_not_observed(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.driver.is_present.side_effect = lambda locator: locator in (
            xp.BROWSER_VIEW, xp.COLLECTION_PICKER, xp.COLLECTION_NAME,
        )
        name_elem = Mock()
        name_elem.get_attribute.return_value = 'Silent Collection'
        self.app.driver.get_element.return_value = name_elem
        self.app.driver.wait_until.side_effect = _make_fake_wait_until()
        self.app.driver.wait_until_present.return_value = False

        self.assertFalse(self.app.save_to_new_collection('Silent Collection'))
        self.assertTrue(any('stage=AMBIGUOUS' in msg for msg in self.app.gtl_logger.messages))

    def test_open_tab_suppresses_duplicate_tab_on_retry_after_click(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.current_state = Firefox.tabs_state
        self.app.driver.is_present.side_effect = lambda locator: locator in (xp.TABS_MENU, xp.TABS_ADD_NORMAL)
        self.app.driver.wait_until.side_effect = TimeoutError('Timed out waiting for blank tab')

        with self.assertRaises(FirefoxAmbiguousActionError):
            self.app.open_tab(private=False)

        add_clicks = [call for call in self.app.driver.click.call_args_list if call.args == (xp.TABS_ADD_NORMAL,)]
        self.assertEqual(len(add_clicks), 1)
        self.assertTrue(any('stage=RETRY_SUPPRESSED' in msg for msg in self.app.gtl_logger.messages))

    def test_close_tab_postcondition_failure_returns_false_and_logs_ambiguous(self):
        self.app.gtl_logger = _RecordingLogger()
        element = Mock()
        self.app.driver.is_present.return_value = True
        self.app.driver.get_elements.return_value = [element]
        self.app.driver.wait_until_not_present.return_value = False

        self.assertFalse(self.app.close_tab('Stubborn Tab'))
        element.click.assert_called_once_with()
        self.assertTrue(any('stage=AMBIGUOUS' in msg for msg in self.app.gtl_logger.messages))

    def test_close_tab_retry_confirms_postcondition_without_second_click(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.current_state = Firefox.tabs_state
        locator = xp.close_tab_button('Transient Tab')
        state = {'tab_present': True}
        button = Mock()

        def click_close():
            state['tab_present'] = False

        button.click.side_effect = click_close
        self.app.driver.is_present.side_effect = lambda loc: (
            state['tab_present'] if loc == locator else loc in (xp.TABS_MENU, xp.TABS_ADD_NORMAL)
        )
        self.app.driver.get_elements.return_value = [button]
        self.app.driver.wait_until_not_present.side_effect = RuntimeError('Transient error after Close click')

        self.assertTrue(self.app.close_tab('Transient Tab'))
        button.click.assert_called_once_with()
        self.assertTrue(any('Postcondition confirmed on retry' in msg for msg in self.app.gtl_logger.messages))

    def test_close_tab_retry_raises_ambiguous_if_tab_still_present(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.current_state = Firefox.tabs_state
        button = Mock()
        self.app.driver.is_present.return_value = True
        self.app.driver.get_elements.return_value = [button]
        self.app.driver.wait_until_not_present.side_effect = RuntimeError('Driver timeout after click')

        with self.assertRaises(FirefoxAmbiguousActionError):
            self.app.close_tab('Persistent Tab')
        button.click.assert_called_once_with()

    def test_add_bookmark_retry_confirms_edit_bookmark_without_second_click(self):
        self.app.gtl_logger = _RecordingLogger()
        state = {'bookmarked': False, 'menu_open': False}

        def is_present(locator: str) -> bool:
            if locator in (xp.BROWSER_NORMAL, xp.BROWSER_ADDRESS, xp.BROWSER_VIEW):
                return True
            if locator == xp.BROWSER_ERROR:
                return False
            if locator == xp.MENU_CLOSE:
                return state['menu_open']
            if locator == xp.MENU_ADD_BOOKMARK:
                return state['menu_open'] and not state['bookmarked']
            if locator == xp.MENU_EDIT_BOOKMARK:
                return state['menu_open'] and state['bookmarked']
            return False

        def click(locator: str) -> None:
            if locator == xp.BROWSER_MENU:
                state['menu_open'] = True
            elif locator == xp.MENU_ADD_BOOKMARK:
                state['bookmarked'] = True
                state['menu_open'] = False

        self.app.driver.is_present.side_effect = is_present
        self.app.driver.click.side_effect = click
        self.app.driver.wait_until_present.return_value = True
        self.app.driver.wait_until_not_present.side_effect = RuntimeError('Driver drop after Bookmark click')

        self.assertTrue(self.app.add_bookmark())
        add_clicks = [c for c in self.app.driver.click.call_args_list if c.args == (xp.MENU_ADD_BOOKMARK,)]
        self.assertEqual(len(add_clicks), 1)
        self.assertTrue(any('Postcondition confirmed on retry' in msg for msg in self.app.gtl_logger.messages))

    def test_delete_bookmark_postcondition_and_retry_without_second_click(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.current_state = Firefox.bookmarks_state
        url = 'https://example.org/03-bookmark'
        locator = xp.bookmark_item_menu(url)
        state = {'present': True}

        def is_present(loc: str) -> bool:
            if loc == locator:
                return state['present']
            return loc == xp.BOOKMARKS_TOOLBAR

        def click(loc: str) -> None:
            if loc == xp.BOOKMARKS_MENU_DELETE:
                state['present'] = False

        self.app.driver.is_present.side_effect = is_present
        self.app.driver.get_elements.return_value = [Mock()]
        self.app.driver.click.side_effect = click
        self.app.driver.wait_until_not_present.side_effect = RuntimeError('Transient error after Delete click')

        self.assertTrue(self.app.delete_bookmark(url))
        delete_clicks = [c for c in self.app.driver.click.call_args_list if c.args == (xp.BOOKMARKS_MENU_DELETE,)]
        self.assertEqual(len(delete_clicks), 1)
        self.assertTrue(any('Postcondition confirmed on retry' in msg for msg in self.app.gtl_logger.messages))

    def test_download_pdf_timeout_and_retry_suppression(self):
        self.app.gtl_logger = _RecordingLogger()
        state = self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/home',
            initial_title='Home',
            polls=[{'address': '127.0.0.1:8080/run/file.pdf', 'title': 'file.pdf'}],
        )
        orig_wait = self.app.driver.wait_until.side_effect

        def wait_with_pdf_timeout(condition, timeout=5, interval=0.5, description=None):
            if description == 'Firefox PDF Download button':
                raise TimeoutError('Download button missing')
            return orig_wait(condition, timeout=timeout, interval=interval, description=description)

        self.app.driver.wait_until.side_effect = wait_with_pdf_timeout

        with self.assertRaises(FirefoxNavigationError):
            self.app.download_pdf('http://127.0.0.1:8080/run/file.pdf')

        # Must not re-submit the PDF URL on PUMA @action retry
        self.assertEqual(state['press_enter_calls'], 1)
        self.assertTrue(any('stage=RETRY_SUPPRESSED' in msg for msg in self.app.gtl_logger.messages))

    def test_download_pdf_suppresses_duplicate_download_click_on_retry(self):
        self.app.gtl_logger = _RecordingLogger()
        self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/home',
            initial_title='Home',
            polls=[{'address': '127.0.0.1:8080/run/file.pdf', 'title': 'file.pdf'}],
        )
        orig_is_present = self.app.driver.is_present.side_effect
        orig_click = self.app.driver.click.side_effect
        pdf_clicks = {'count': 0}

        def is_present(locator: str) -> bool:
            if locator == xp.BROWSER_PDF_DOWNLOAD:
                return True
            return orig_is_present(locator)

        def click(locator: str) -> None:
            if locator == xp.BROWSER_PDF_DOWNLOAD:
                pdf_clicks['count'] += 1
                raise PumaClickException('Click response dropped after tap')
            orig_click(locator)

        self.app.driver.is_present.side_effect = is_present
        self.app.driver.click.side_effect = click

        with self.assertRaises(FirefoxAmbiguousActionError):
            self.app.download_pdf('http://127.0.0.1:8080/run/file.pdf')

        self.assertEqual(pdf_clicks['count'], 1)

    # ------------------------------------------------------------------
    # P1: Tracking-protection attribution
    # ------------------------------------------------------------------

    def test_parse_site_info_ignores_unrelated_on_off_settings_before_tp(self):
        root = ElementTree.fromstring(
            '<hierarchy>'
            '  <node resource-id="unified.trust.panel.website.url" text="127.0.0.1"/>'
            '  <node class="android.view.View">'
            '    <node text="Cookies"/>'
            '    <node text="On"/>'
            '  </node>'
            '  <node class="android.view.View">'
            '    <node text="Location"/>'
            '    <node text="On"/>'
            '  </node>'
            '  <node class="android.view.View">'
            '    <node text="Enhanced Tracking Protection"/>'
            '    <node text="Off"/>'
            '  </node>'
            '</hierarchy>'
        )
        self.assertEqual(_parse_site_info(root), {'url': '127.0.0.1', 'tracking_protection': 'Off'})

    def test_parse_site_info_returns_unknown_when_tp_absent_despite_unrelated_on_off(self):
        root = ElementTree.fromstring(
            '<hierarchy>'
            '  <node resource-id="unified.trust.panel.website.url" text="example.org"/>'
            '  <node class="android.view.View">'
            '    <node text="Autoplay"/>'
            '    <node text="On"/>'
            '  </node>'
            '</hierarchy>'
        )
        self.assertEqual(_parse_site_info(root), {'url': 'example.org', 'tracking_protection': 'unknown'})

    def test_parse_site_info_reads_checkable_switch_and_merged_compose_node(self):
        switch_root = ElementTree.fromstring(
            '<hierarchy>'
            '  <node resource-id="unified.trust.panel.website.url" text="example.org"/>'
            '  <node class="android.view.View">'
            '    <node text="Enhanced Tracking Protection"/>'
            '    <node class="android.widget.Switch" checkable="true" checked="true"/>'
            '  </node>'
            '</hierarchy>'
        )
        self.assertEqual(_parse_site_info(switch_root), {'url': 'example.org', 'tracking_protection': 'On'})

        merged_root = ElementTree.fromstring(
            '<hierarchy>'
            '  <node resource-id="unified.trust.panel.website.url" text="example.org"/>'
            '  <node content-desc="Enhanced Tracking Protection, Off"/>'
            '</hierarchy>'
        )
        self.assertEqual(_parse_site_info(merged_root), {'url': 'example.org', 'tracking_protection': 'Off'})

    def test_parse_site_info_returns_ambiguous_on_conflicting_states(self):
        root = ElementTree.fromstring(
            '<hierarchy>'
            '  <node resource-id="unified.trust.panel.website.url" text="example.org"/>'
            '  <node class="android.view.View">'
            '    <node text="Enhanced Tracking Protection"/>'
            '    <node text="On"/>'
            '    <node class="android.widget.Switch" checkable="true" checked="false"/>'
            '  </node>'
            '</hierarchy>'
        )
        self.assertEqual(_parse_site_info(root), {'url': 'example.org', 'tracking_protection': 'ambiguous'})

    def test_inspect_site_info_always_dismisses_panel_on_parse_error(self):
        self.app.gtl_logger = _RecordingLogger()
        self.app.recover_state = Mock()
        self.app.driver.is_present.side_effect = lambda locator: locator in (xp.BROWSER_SITE_INFO, xp.SITE_INFO_URL)
        self.app.driver.wait_until.side_effect = _make_fake_wait_until()
        self.app.driver.driver.page_source = '<invalid-xml'

        with self.assertRaises(ElementTree.ParseError):
            self.app.inspect_site_info()

        self.assertGreaterEqual(self.app.driver.back.call_count, 1)

    # ------------------------------------------------------------------
    # P1: Provenance logging & verify_with semantics
    # ------------------------------------------------------------------

    def test_provenance_logs_utc_timestamps_sequence_ids_and_stages(self):
        self.app.gtl_logger = _RecordingLogger()
        self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/old',
            initial_title='Old',
            polls=[{'address': '127.0.0.1:8080/new', 'title': 'New'}],
        )

        self.app.visit_url('http://127.0.0.1:8080/new', expected_title='New')

        iso_re = re.compile(r'ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 action_id=(ff-\d{4}) stage=(\w+)')
        parsed = [m.groups() for msg in self.app.gtl_logger.messages if (m := iso_re.search(msg))]
        self.assertEqual([stage for _, stage in parsed], ['INTENDED', 'ATTEMPTED', 'OBSERVED', 'VERIFIED'])
        self.assertEqual(len({aid for aid, _ in parsed}), 1)

    def test_verify_with_logs_failure_without_raising(self):
        self.app.gtl_logger = _RecordingLogger()
        self._configure_navigation_driver(
            initial_address='127.0.0.1:8080/old',
            initial_title='Old',
            polls=[{'address': '127.0.0.1:8080/new', 'title': 'Actual Title'}],
        )

        def wrong_title() -> bool:
            return False

        observed = self.app.visit_url('http://127.0.0.1:8080/new', verify_with=wrong_title)
        self.assertEqual(observed, '127.0.0.1:8080/new')
        self.assertIn("Action 'visit_url' failed", self.app.gtl_logger.messages)

    # ------------------------------------------------------------------
    # P2: XPath validity, quoting edge cases, & deterministic runner checks
    # ------------------------------------------------------------------

    def test_all_xpath_constants_and_builders_are_valid_xpath(self):
        candidates = [value for name, value in vars(xp).items() if name.isupper() and isinstance(value, str)]
        candidates.extend([
            xp.browser_view_with_title("""Title with 'single' and "double" quotes"""),
            xp.browser_view_with_text("Body text [1]"),
            xp.close_tab_button("Tab's Title"),
            xp.tab_card_for_close_button(xp.close_tab_button('Sample')),
            xp.bookmark_item_menu("https://example.org/a'b?x=1&y=2"),
            xp.collection_name('PUMA Collection 09'),
            xp.download_filename('PUMA-Firefox-06.pdf'),
        ])
        for expr in candidates:
            with self.subTest(xpath=expr):
                self.assertIsNotNone(elementpath.Selector(expr))

    def test_xpath_literal_edge_cases(self):
        self.assertEqual(xp.literal(''), "''")
        self.assertEqual(xp.literal("plain"), "'plain'")
        self.assertEqual(xp.literal("it's"), '"it\'s"')
        self.assertEqual(xp.literal('say "hi"'), '\'say "hi"\'')
        self.assertEqual(xp.literal("""a'b"c'd"""), """concat('a', "'", 'b"c', "'", 'd')""")

    def test_runner_rejects_output_inside_repository(self):
        repo_root = Path(firefox_runner.__file__).resolve().parents[1]
        inside_output = repo_root / 'logs' / 'forbidden-capture-dir'
        stderr = io.StringIO()
        with patch.object(firefox_runner, '_adb') as mock_adb, redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                firefox_runner.main(['--device', 'FAKE-SERIAL', '--output', str(inside_output)])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('outside the source checkout', stderr.getvalue())
        mock_adb.assert_not_called()

    def test_runner_local_http_fixture_serves_html_query_and_pdf_with_sha256(self):
        with firefox_runner._local_http_fixture('test-run-01') as (_, base, titles, filename, digest):
            with urlopen(f'{base}/01-visit', timeout=5) as resp:
                body = resp.read().decode('utf-8')
                self.assertEqual(resp.status, 200)
                self.assertIn(titles['01-visit'], body)

            with urlopen(f'{base}/02-search-fixture?q=PUMA+%3Cscript%3E', timeout=5) as resp:
                body = resp.read().decode('utf-8')
                self.assertEqual(resp.status, 200)
                self.assertIn('PUMA Fixture Search: PUMA &lt;script&gt;', body)
                self.assertNotIn('<script>', body)

            with urlopen(f'{base}/{filename}', timeout=5) as resp:
                pdf_bytes = resp.read()
                self.assertEqual(resp.status, 200)
                self.assertTrue(pdf_bytes.startswith(b'%PDF-1.4'))
                self.assertEqual(sha256(pdf_bytes).hexdigest(), digest)

            with self.assertRaises(HTTPError) as err:
                urlopen(f'{base}/does-not-exist', timeout=5)
            self.assertEqual(err.exception.code, 404)

    def test_runner_utc_formatter_emits_timezone_aware_iso_timestamp(self):
        formatter = firefox_runner._UtcIsoFormatter('%(asctime)s [%(levelname)s] %(message)s')
        record = logging.LogRecord('test', logging.INFO, __file__, 1, 'hello', (), None)
        formatted = formatter.format(record)
        self.assertRegex(formatted, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00 \[INFO\] hello$')


if __name__ == '__main__':
    unittest.main()
