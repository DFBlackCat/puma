"""Puma StateGraph for Firefox on Android."""

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree

from puma.apps.android.appium_actions import supported_version
from puma.apps.android.firefox import xpaths as xp
from puma.state_graph.action import action
from puma.state_graph.popup_handler import PopUpHandler
from puma.state_graph.puma_driver import PumaDriver
from puma.state_graph.state import SimpleState, State, compose_clicks, scroll_to
from puma.state_graph.state_graph import StateGraph

FIREFOX_PACKAGE = 'org.mozilla.firefox'
_ON_OFF_WORD_RE = re.compile(r'\b(on|off)\b', re.IGNORECASE)


class FirefoxError(RuntimeError):
    """Base error raised by the Firefox StateGraph integration."""


class FirefoxNavigationError(FirefoxError):
    """Raised when Firefox navigation fails, displays an error page, or times out."""


class FirefoxPreconditionError(FirefoxError):
    """Raised when a read-only observation cannot run without mutating browser state."""


class FirefoxAmbiguousActionError(FirefoxError):
    """Raised when an action crossed its commit point and retry is suppressed or outcome is ambiguous."""


@dataclass
class _CommitRecord:
    action_id: str
    action_name: str
    key: tuple
    error: BaseException | None = None
    retry_triggered: bool = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _select_tab_mode(driver: PumaDriver, private: bool) -> None:
    add_tab = xp.TABS_ADD_PRIVATE if private else xp.TABS_ADD_NORMAL
    if not driver.is_present(add_tab):
        driver.click(xp.TABS_SELECT_PRIVATE if private else xp.TABS_SELECT_NORMAL)
        driver.wait_until(lambda: driver.is_present(add_tab), 5, description='Firefox tab mode')


def _open_blank_tab(driver: PumaDriver, private: bool) -> None:
    _select_tab_mode(driver, private)
    driver.click(xp.TABS_ADD_PRIVATE if private else xp.TABS_ADD_NORMAL)
    mode = xp.BROWSER_PRIVATE if private else xp.BROWSER_NORMAL
    driver.wait_until(lambda: driver.is_present(mode), 5, description='Firefox blank tab')


def _enter_browser_from_tabs(driver: PumaDriver, private: bool) -> None:
    """Return from the tab tray to an existing tab without creating a new tab whenever possible."""
    _select_tab_mode(driver, private)
    mode = xp.BROWSER_PRIVATE if private else xp.BROWSER_NORMAL
    mode_name = 'private' if private else 'normal'
    if driver.is_present(xp.TABS_EXISTING_CARD):
        driver.gtl_logger.info(
            'ts=%s stage=OBSERVED action=enter_browser_from_tabs Selecting an existing %s Firefox tab '
            'without creating a new tab',
            _utc_now_iso(), mode_name,
        )
        driver.click(xp.TABS_EXISTING_CARD)
        driver.wait_until(lambda: driver.is_present(mode), 5, description=f'Existing Firefox {mode_name} tab')
        return

    driver.gtl_logger.warning(
        'ts=%s stage=STATE_RECOVERY_MUTATION action=enter_browser_from_tabs No existing %s Firefox tab '
        'visible in tray; unavoidably opening a new blank %s tab to reach browser state',
        _utc_now_iso(), mode_name, mode_name,
    )
    _open_blank_tab(driver, private)


def _enter_normal_browser(driver: PumaDriver) -> None:
    _enter_browser_from_tabs(driver, private=False)


def _enter_private_browser(driver: PumaDriver) -> None:
    _enter_browser_from_tabs(driver, private=True)


def _open_normal_tab(driver: PumaDriver) -> None:
    _open_blank_tab(driver, private=False)


def _open_private_tab(driver: PumaDriver) -> None:
    _open_blank_tab(driver, private=True)


def _return_to_browser(driver: PumaDriver) -> None:
    driver.close_keyboard()
    if not driver.is_present(xp.BROWSER_ADDRESS) and not driver.is_present(xp.TABS_MENU):
        driver.back()
        if driver.wait_until_present(xp.COLLECTION_PICKER, timeout=0.5):
            driver.back()
    driver.wait_until(
        lambda: driver.is_present(xp.BROWSER_ADDRESS) or driver.is_present(xp.TABS_MENU),
        5, description='Firefox browser or tab tray',
    )
    if driver.is_present(xp.TABS_MENU):
        _enter_browser_from_tabs(driver, private=False)
    elif driver.is_present(xp.BROWSER_PRIVATE):
        driver.click(xp.BROWSER_TAB_COUNTER)
        driver.wait_until(lambda: driver.is_present(xp.TABS_MENU), 5, description='Firefox tab tray')
        _enter_browser_from_tabs(driver, private=False)


def _go_to_normal_tabs(driver: PumaDriver) -> None:
    _select_tab_mode(driver, private=False)


def _find_setting(driver: PumaDriver, locator: str) -> None:
    settings = driver.get_element(xp.SETTINGS_LIST)
    for direction in ('down', 'up'):
        for _ in range(5):
            if driver.is_present(locator):
                return
            if not driver.driver.execute_script(
                    'mobile: scrollGesture', {'elementId': settings.id, 'direction': direction, 'percent': 0.75}):
                break
    driver.wait_until(lambda: driver.is_present(locator), 3, description='Firefox preference row')


def _open_site_settings(driver: PumaDriver) -> None:
    _find_setting(driver, xp.SETTINGS_SITE)
    driver.click(xp.SETTINGS_SITE)


def _open_tab_settings(driver: PumaDriver) -> None:
    _find_setting(driver, xp.SETTINGS_TAB)
    driver.click(xp.SETTINGS_TAB)


def _read_history_rows(root: ElementTree.Element) -> list[dict[str, str]]:
    rows = []
    for row in root.iter():
        if row.get('resource-id') != f'{FIREFOX_PACKAGE}:id/history_layout':
            continue
        values = {node.get('resource-id'): node.get('text', '') for node in row.iter()}
        title = values.get(f'{FIREFOX_PACKAGE}:id/title', '')
        url = values.get(f'{FIREFOX_PACKAGE}:id/url', '')
        if title or url:
            rows.append({'title': title, 'url': url})
    return rows


def _address_matches_url(displayed_address: str, target_url: str, *, strict: bool = False) -> bool:
    """Compare Firefox's displayed address label against a target HTTP(S) URL."""
    displayed = displayed_address.strip()
    if not displayed or displayed.casefold() == 'search or enter address':
        return False

    target_parts = urlsplit(target_url)
    target_host = (target_parts.hostname or '').casefold()
    if not target_host:
        return False

    displayed_parts = urlsplit(displayed if '://' in displayed else f'http://{displayed}')
    displayed_host = (displayed_parts.hostname or '').casefold()
    if not displayed_host:
        return False

    if target_parts.port is not None and displayed_parts.port is not None and target_parts.port != displayed_parts.port:
        return False

    if strict:
        if displayed_host != target_host:
            return False
    else:
        allowed_hosts = {target_host, f'www.{target_host}'}
        if target_host.startswith('www.'):
            allowed_hosts.add(target_host.removeprefix('www.'))
        if displayed_host not in allowed_hosts:
            return False

    target_path = target_parts.path.rstrip('/')
    displayed_path = displayed_parts.path.rstrip('/')
    if target_path:
        if displayed_path != target_path:
            return False
    elif strict and displayed_path:
        return False

    if target_parts.query:
        if parse_qsl(displayed_parts.query, keep_blank_values=True) != parse_qsl(
                target_parts.query, keep_blank_values=True):
            return False
    return True


def _is_tracking_protection_marker(node: ElementTree.Element) -> bool:
    text = (node.get('text') or '').casefold()
    desc = (node.get('content-desc') or '').casefold()
    resource_id = (node.get('resource-id') or '').casefold()
    return (
        'tracking protection' in text
        or 'tracking protection' in desc
        or ('tracking' in resource_id and 'protection' in resource_id)
    )


def _node_states(node: ElementTree.Element, *, include_desc_words: bool = False) -> set[str]:
    states: set[str] = set()
    text = (node.get('text') or '').strip()
    if text in ('On', 'Off'):
        states.add(text)
    if (node.get('checkable') or '').casefold() == 'true':
        checked = (node.get('checked') or '').casefold()
        if checked == 'true':
            states.add('On')
        elif checked == 'false':
            states.add('Off')
    if include_desc_words or (node.get('checkable') or '').casefold() == 'true':
        desc = node.get('content-desc') or ''
        for match in _ON_OFF_WORD_RE.findall(desc):
            states.add('On' if match.casefold() == 'on' else 'Off')
    return states


def _subtree_contains(container: ElementTree.Element, target: ElementTree.Element) -> bool:
    return any(node is target for node in container.iter())


def _is_unrelated_setting_subtree(subtree: ElementTree.Element) -> bool:
    has_non_tp_label = False
    has_state = False
    for node in subtree.iter():
        if _is_tracking_protection_marker(node):
            return False
        if _node_states(node):
            has_state = True
            continue
        label = (node.get('text') or node.get('content-desc') or '').strip()
        if label and label not in ('On', 'Off'):
            has_non_tp_label = True
    return has_non_tp_label and has_state


def _parse_site_info(root: ElementTree.Element) -> dict[str, str]:
    """Parse website URL and strictly scoped Enhanced Tracking Protection status from site info XML."""
    url = next(
        (node.get('text', '') for node in root.iter()
         if node.get('resource-id') == 'unified.trust.panel.website.url'),
        '',
    )
    parent_map = {child: parent for parent in root.iter() for child in parent}
    markers = [node for node in root.iter() if _is_tracking_protection_marker(node)]
    if not markers:
        return {'url': url, 'tracking_protection': 'unknown'}

    resolved: set[str] = set()
    ambiguous = False

    for marker in markers:
        direct_states = _node_states(marker, include_desc_words=True)
        if len(direct_states) > 1:
            ambiguous = True
            continue
        if len(direct_states) == 1:
            resolved.update(direct_states)
            continue

        container = parent_map.get(marker)
        for _ in range(2):
            if container is None or container is root:
                break
            if any(node.get('resource-id') == 'unified.trust.panel.website.url' for node in container.iter()):
                break

            sibling_settings = [
                child for child in container
                if not _subtree_contains(child, marker) and _is_unrelated_setting_subtree(child)
            ]
            if sibling_settings:
                break

            states: set[str] = set()
            for node in container.iter():
                states.update(_node_states(node, include_desc_words=_is_tracking_protection_marker(node)))
            if len(states) > 1:
                ambiguous = True
                break
            if len(states) == 1:
                resolved.update(states)
                break
            container = parent_map.get(container)

    if ambiguous or len(resolved) > 1:
        status = 'ambiguous'
    elif len(resolved) == 1:
        status = next(iter(resolved))
    else:
        status = 'unknown'
    return {'url': url, 'tracking_protection': status}


@supported_version('160.0.1')
class Firefox(StateGraph):
    """Drive Firefox's normal and private browsing surfaces and inspect its browser data over the UI."""

    browser_state = SimpleState([xp.BROWSER_NORMAL, xp.BROWSER_ADDRESS], initial_state=True)
    private_browser_state = SimpleState([xp.BROWSER_PRIVATE, xp.BROWSER_ADDRESS])
    tabs_state = SimpleState([xp.TABS_MENU], invalid_xpaths=[xp.TABS_GROUPS_SELECTED, xp.TABS_SYNCED_SELECTED])
    tabs_menu_state = SimpleState([xp.TABS_MENU_RECENT, xp.TABS_MENU_SETTINGS], parent_state=tabs_state,
                                  recognize_only=True)
    groups_state = SimpleState([xp.TABS_GROUPS_SELECTED], parent_state=tabs_state,
                               parent_state_transition=_go_to_normal_tabs, recognize_only=True)
    synced_tabs_state = SimpleState([xp.TABS_SYNCED_SELECTED], parent_state=tabs_state,
                                    parent_state_transition=_go_to_normal_tabs, recognize_only=True)
    address_edit_state = SimpleState([xp.BROWSER_EDIT], parent_state=browser_state,
                                     parent_state_transition=_return_to_browser, recognize_only=True)
    menu_state = SimpleState([xp.MENU_CLOSE], parent_state=browser_state, parent_state_transition=_return_to_browser)
    bookmarks_state = SimpleState([xp.BOOKMARKS_TOOLBAR], parent_state=browser_state,
                                  parent_state_transition=_return_to_browser)
    bookmark_menu_state = SimpleState([xp.BOOKMARKS_MENU_PRIVATE, xp.BOOKMARKS_MENU_DELETE],
                                      parent_state=bookmarks_state, recognize_only=True)
    history_state = SimpleState([xp.HISTORY_LAYOUT], parent_state=browser_state,
                                parent_state_transition=_return_to_browser)
    recently_closed_state = SimpleState([xp.RECENTLY_CLOSED_LAYOUT], parent_state=history_state)
    downloads_state = SimpleState([xp.DOWNLOADS_SETTINGS], parent_state=browser_state,
                                  parent_state_transition=_return_to_browser)
    settings_state = SimpleState([xp.SETTINGS_SEARCH], parent_state=browser_state,
                                 parent_state_transition=_return_to_browser)
    site_settings_state = SimpleState([xp.SITE_SETTINGS_TITLE], parent_state=settings_state)
    tab_settings_state = SimpleState([xp.TAB_SETTINGS_TITLE], parent_state=settings_state)
    delete_data_state = SimpleState([xp.DELETE_DATA_TITLE], parent_state=settings_state, recognize_only=True)
    site_info_state = SimpleState([xp.SITE_INFO_URL], parent_state=browser_state,
                                  parent_state_transition=_return_to_browser, recognize_only=True)
    collection_picker_state = SimpleState([xp.COLLECTION_PICKER], parent_state=browser_state,
                                          parent_state_transition=_return_to_browser, recognize_only=True)

    tabs_state.from_states([browser_state, private_browser_state],
                           compose_clicks([xp.BROWSER_TAB_COUNTER], 'open_tab_tray'))
    tabs_state.to(browser_state, _enter_normal_browser)
    tabs_state.to(private_browser_state, _enter_private_browser)
    menu_state.from_states([browser_state, private_browser_state], compose_clicks([xp.BROWSER_MENU], 'open_menu'))
    menu_state.to(bookmarks_state, compose_clicks([scroll_to(xp.MENU_BOOKMARKS, max_swipes=5)], 'open_bookmarks'))
    menu_state.to(history_state, compose_clicks([scroll_to(xp.MENU_HISTORY, max_swipes=5)], 'open_history'))
    menu_state.to(downloads_state, compose_clicks([scroll_to(xp.MENU_DOWNLOADS, max_swipes=5)], 'open_downloads'))
    menu_state.to(settings_state, compose_clicks([scroll_to(xp.MENU_SETTINGS, max_swipes=5)], 'open_settings'))
    history_state.to(recently_closed_state, compose_clicks([xp.HISTORY_RECENTLY_CLOSED], 'open_recently_closed'))
    settings_state.to(site_settings_state, _open_site_settings)
    settings_state.to(tab_settings_state, _open_tab_settings)

    def __init__(self, device_udid: str, **kwargs) -> None:
        """Connect to Firefox without resetting its profile.

        :param device_udid: Android device identifier.
        :param kwargs: Arguments forwarded to :class:`StateGraph`.
        """
        super().__init__(device_udid, FIREFOX_PACKAGE, **kwargs)
        self.add_popup_handlers(
            PopUpHandler([xp.POPUP_DEFAULT_TITLE], [xp.POPUP_DEFAULT_CANCEL]),
            PopUpHandler([xp.POPUP_DEFAULT_OFFER], [xp.POPUP_DEFAULT_SKIP]),
        )
        self._log_provenance(
            'OBSERVED', 'init',
            f'Initialized Firefox StateGraph package={FIREFOX_PACKAGE} supported_version={self.supported_version}',
        )

    def _next_action_id(self) -> str:
        seq = self.__dict__.get('_action_sequence', 0) + 1
        self.__dict__['_action_sequence'] = seq
        return f'ff-{seq:04d}'

    def _get_pending_commit(self) -> _CommitRecord | None:
        return self.__dict__.get('_pending_commit')

    def _set_pending_commit(self, record: _CommitRecord | None) -> None:
        self.__dict__['_pending_commit'] = record

    def _consume_retry_commit(self, action_name: str, key: tuple) -> _CommitRecord | None:
        record = self._get_pending_commit()
        if record is None:
            return None
        if record.retry_triggered and record.action_name == action_name and record.key == key:
            self._set_pending_commit(None)
            return record
        # Clear any stale record from an unrelated prior call.
        self._set_pending_commit(None)
        return None

    def _log_provenance(
        self,
        stage: str,
        action_name: str,
        detail: str,
        *,
        action_id: str | None = None,
        level: int = logging.INFO,
    ) -> str:
        aid = action_id or self._next_action_id()
        message = f'ts={_utc_now_iso()} action_id={aid} stage={stage} action={action_name} {detail}'
        if level >= logging.ERROR:
            self.gtl_logger.error(message)
        elif level >= logging.WARNING:
            self.gtl_logger.warning(message)
        else:
            self.gtl_logger.info(message)
        return aid

    def recover_state(self, expected_state: State) -> None:
        """Recover StateGraph state while marking any in-flight commit point for retry safety.

        :param expected_state: The state expected by PUMA StateGraph recovery.
        """
        if 'current_state' not in self.__dict__:
            self.current_state = self.initial_state
        record = self._get_pending_commit()
        if record is not None and not record.retry_triggered:
            record.retry_triggered = True
        try:
            super().recover_state(expected_state)
        except Exception:
            self._set_pending_commit(None)
            raise

    @action(browser_state)
    def visit_url(
        self,
        url: str,
        expected_url: str | None = None,
        expected_title: str | None = None,
        expected_text: str | None = None,
        timeout: float = 20,
    ) -> str:
        """Open an HTTP(S) URL in the normal browsing surface.

        :param url: Absolute HTTP or HTTPS URL.
        :param expected_url: Optional expected destination URL after redirects.
        :param expected_title: Optional exact accessible page title required for completion.
        :param expected_text: Optional visible text substring required inside the WebView.
        :param timeout: Maximum seconds to wait for destination observation.
        :return: Address as displayed by Firefox after navigation observation.
        """
        self._check_url(url)
        if expected_url is not None:
            self._check_url(expected_url)
        return self._submit_address(
            url,
            action_name='visit_url',
            is_url=True,
            expected_url=expected_url,
            expected_title=expected_title,
            expected_text=expected_text,
            timeout=timeout,
        )

    @action(private_browser_state)
    def visit_url_private(
        self,
        url: str,
        expected_url: str | None = None,
        expected_title: str | None = None,
        expected_text: str | None = None,
        timeout: float = 20,
    ) -> str:
        """Open an HTTP(S) URL in the private browsing surface.

        :param url: Absolute HTTP or HTTPS URL.
        :param expected_url: Optional expected destination URL after redirects.
        :param expected_title: Optional exact accessible page title required for completion.
        :param expected_text: Optional visible text substring required inside the WebView.
        :param timeout: Maximum seconds to wait for destination observation.
        :return: Address as displayed by Firefox after navigation observation.
        """
        self._check_url(url)
        if expected_url is not None:
            self._check_url(expected_url)
        return self._submit_address(
            url,
            action_name='visit_url_private',
            is_url=True,
            expected_url=expected_url,
            expected_title=expected_title,
            expected_text=expected_text,
            timeout=timeout,
        )

    @action(browser_state)
    def search(
        self,
        query: str,
        expected_title: str | None = None,
        expected_text: str | None = None,
        timeout: float = 20,
    ) -> str:
        """Submit a query using Firefox's current default search engine.

        :param query: Search terms to type.
        :param expected_title: Optional exact accessible page title required for completion.
        :param expected_text: Optional visible text substring required inside the WebView.
        :param timeout: Maximum seconds to wait for search result observation.
        :return: Address as displayed by Firefox after submission observation.
        """
        if not query.strip():
            raise ValueError('A search query is required')
        return self._submit_address(
            query,
            action_name='search',
            is_url=False,
            expected_title=expected_title,
            expected_text=expected_text,
            timeout=timeout,
        )

    @staticmethod
    def _check_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Firefox requires an absolute HTTP or HTTPS URL')

    def _current_page_title(self) -> str:
        if not self.driver.is_present(xp.BROWSER_VIEW):
            return ''
        try:
            title = self.driver.get_element(xp.BROWSER_VIEW).get_attribute('text')
        except Exception:
            return ''
        return title if isinstance(title, str) else ''

    def _submit_address(
        self,
        value: str,
        *,
        action_name: str,
        is_url: bool,
        expected_url: str | None = None,
        expected_title: str | None = None,
        expected_text: str | None = None,
        timeout: float = 20,
        clear_commit_on_success: bool = True,
    ) -> str:
        commit_key = (value, expected_url, expected_title, expected_text)
        retry_record = self._consume_retry_commit(action_name, commit_key)
        if retry_record is not None:
            self._log_provenance(
                'RETRY_SUPPRESSED', action_name,
                f'Suppressing automatic re-submission of {value!r} after commit point to prevent duplicate navigation',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            if isinstance(retry_record.error, FirefoxNavigationError):
                raise retry_record.error
            raise FirefoxAmbiguousActionError(
                f'{action_name} failed after submitting {value!r}; automatic retry suppressed to avoid duplicate '
                f'navigation'
            ) from retry_record.error

        action_id = self._log_provenance(
            'INTENDED', action_name,
            f'target={value!r} expected_url={expected_url!r} expected_title={expected_title!r} '
            f'expected_text={expected_text!r}',
        )
        previous_address = self._displayed_address() if self.driver.is_present(xp.BROWSER_ADDRESS) else ''
        previous_title = self._current_page_title()

        self.driver.click(xp.BROWSER_ADDRESS)
        self.driver.wait_until(
            lambda: self.driver.is_present(xp.BROWSER_EDIT), 5, description='Firefox address editor',
        )
        self.driver.send_keys(xp.BROWSER_EDIT, value)
        self.driver.wait_until(
            lambda: self.driver.get_element(xp.BROWSER_EDIT).get_attribute('text') == value,
            5, interval=0.25, description='Firefox address field contains the submitted text',
        )

        record = _CommitRecord(action_id=action_id, action_name=action_name, key=commit_key)
        self._set_pending_commit(record)
        self._log_provenance(
            'ATTEMPTED', action_name, f'Submitting Firefox address field with {value!r}', action_id=action_id,
        )
        try:
            self.driver.press_enter()
            target_url = expected_url or value
            revisit_same_url = bool(
                is_url and previous_address and _address_matches_url(previous_address, target_url, strict=expected_url is not None)
            )
            error_detected = False
            observed_address = ''

            def _destination_reached() -> bool:
                nonlocal error_detected, observed_address
                if self.driver.is_present(xp.POPUP_NOTIFICATIONS):
                    self.driver.back()
                if self.driver.is_present(xp.BROWSER_ERROR):
                    error_detected = True
                    return True
                if (self.driver.is_present(xp.BROWSER_EDIT)
                        or not self.driver.is_present(xp.BROWSER_VIEW)
                        or not self.driver.is_present(xp.BROWSER_ADDRESS)):
                    return False

                observed_address = self._displayed_address()
                if is_url:
                    if not _address_matches_url(observed_address, target_url, strict=expected_url is not None):
                        return False
                else:
                    if not observed_address or observed_address.casefold() == 'search or enter address':
                        return False
                    if expected_title is None and expected_text is None:
                        current_title = self._current_page_title()
                        if (value.casefold() not in observed_address.casefold()
                                and observed_address == previous_address
                                and (not current_title or current_title == previous_title)):
                            return False

                if expected_title is not None and not self.driver.is_present(xp.browser_view_with_title(expected_title)):
                    return False
                if expected_text is not None and not self.driver.is_present(xp.browser_view_with_text(expected_text)):
                    return False
                return True

            try:
                self.driver.wait_until(
                    _destination_reached, timeout, interval=0.5, description='Firefox destination navigation',
                )
            except TimeoutError as exc:
                self._log_provenance(
                    'FAILED', action_name,
                    f'Navigation timed out for {value!r}; last_observed_address={observed_address!r} '
                    f'(UI interaction completed, destination not confirmed)',
                    action_id=action_id, level=logging.ERROR,
                )
                raise FirefoxNavigationError(
                    f'Timed out waiting for Firefox navigation to {value!r}; '
                    f'last observed address: {observed_address!r}'
                ) from exc

            if error_detected or self.driver.is_present(xp.BROWSER_ERROR):
                self._log_provenance(
                    'FAILED', action_name,
                    f'Firefox displayed browser error page for {value!r}',
                    action_id=action_id, level=logging.ERROR,
                )
                raise FirefoxNavigationError(f'Firefox displayed a browser error page for {value!r}')

            address = observed_address or self._displayed_address()
            revisit_note = (
                ' (prior address already matched target; UI address alone does not distinguish reload from prior page)'
                if revisit_same_url and expected_title is None and expected_text is None else ''
            )
            self._log_provenance(
                'OBSERVED', action_name,
                f'UI address bar displays {address!r} (UI observation only; does not confirm disk persistence)'
                f'{revisit_note}',
                action_id=action_id,
            )
            if expected_url is not None or expected_title is not None or expected_text is not None:
                self._log_provenance(
                    'VERIFIED', action_name,
                    f'Explicit UI destination constraint matched for {value!r} (address={address!r})',
                    action_id=action_id,
                )
            if clear_commit_on_success:
                self._set_pending_commit(None)
            return address
        except BaseException as exc:
            record.error = exc
            raise

    def _handle_popups(self) -> None:
        if self.driver.is_present(xp.POPUP_NOTIFICATIONS):
            self.driver.back()
            return
        super()._handle_popups()

    def _displayed_address(self) -> str:
        label = self.driver.get_element(xp.BROWSER_ADDRESS).get_attribute('content-desc') or ''
        return label.strip().removesuffix('. Search or enter address')

    def is_page_title(self, title: str, timeout: float = 12) -> bool:
        """Check the current page title without changing tabs.

        :param title: Exact accessible page title.
        :param timeout: Maximum seconds to wait for the title.
        :return: Whether Firefox exposes that title in the WebView UI.
        """
        return bool(self.driver.wait_until_present(xp.browser_view_with_title(title), timeout=timeout))

    def has_page_text(self, text: str, timeout: float = 12) -> bool:
        """Check for visible text inside the current WebView without changing tabs.

        :param text: Expected text substring in the WebView hierarchy.
        :param timeout: Maximum seconds to wait for the text.
        :return: Whether Firefox exposes that text in the WebView UI.
        """
        return bool(self.driver.wait_until_present(xp.browser_view_with_text(text), timeout=timeout))

    @action(tabs_state)
    def open_tab(self, private: bool = False) -> None:
        """Open a blank tab in the selected browsing mode.

        :param private: Whether to open a private tab.
        """
        commit_key = (private,)
        retry_record = self._consume_retry_commit('open_tab', commit_key)
        if retry_record is not None:
            self._log_provenance(
                'RETRY_SUPPRESSED', 'open_tab',
                f'Suppressing retry of open_tab(private={private}) after Add-tab click to prevent duplicate tab',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            raise FirefoxAmbiguousActionError(
                f'open_tab(private={private}) failed after clicking Add tab; automatic retry suppressed'
            ) from retry_record.error

        action_id = self._log_provenance('INTENDED', 'open_tab', f'private={private}')
        _select_tab_mode(self.driver, private)
        record = _CommitRecord(action_id=action_id, action_name='open_tab', key=commit_key)
        self._set_pending_commit(record)
        self._log_provenance('ATTEMPTED', 'open_tab', f'Clicking Add tab (private={private})', action_id=action_id)
        try:
            self.driver.click(xp.TABS_ADD_PRIVATE if private else xp.TABS_ADD_NORMAL)
            mode = xp.BROWSER_PRIVATE if private else xp.BROWSER_NORMAL
            self.driver.wait_until(lambda: self.driver.is_present(mode), 5, description='Firefox blank tab')
            self.current_state = self.private_browser_state if private else self.browser_state
            self._set_pending_commit(None)
            self._log_provenance(
                'OBSERVED', 'open_tab', f'Opened new blank tab (private={private})', action_id=action_id,
            )
        except BaseException as exc:
            record.error = exc
            raise

    @action(tabs_state)
    def inspect_tabs(self, private: bool = False) -> list[str]:
        """Read the visible tab titles in the selected mode without mutating tabs.

        :param private: Whether to inspect private tabs.
        :return: Visible titles in UI order, including duplicate titles.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_tabs', f'private={private} read_only=True')
        _select_tab_mode(self.driver, private)
        root = ElementTree.fromstring(self.driver.driver.page_source)
        titles = [desc.removeprefix('Close tab ') for node in root.iter('android.widget.Button')
                  if (desc := node.get('content-desc', '')).startswith('Close tab ')]
        self._log_provenance(
            'OBSERVED', 'inspect_tabs', f'private={private} visible_tab_count={len(titles)}', action_id=action_id,
        )
        return titles

    def _tab_button(self, title: str, private: bool) -> str:
        _select_tab_mode(self.driver, private)
        return xp.close_tab_button(title)

    @action(tabs_state)
    def switch_tab(self, title: str, private: bool = False) -> bool:
        """Switch to a uniquely named visible tab.

        :param title: Exact accessible title of the tab.
        :param private: Whether to select from private tabs.
        :return: Whether a unique visible tab was selected.
        """
        action_id = self._log_provenance('INTENDED', 'switch_tab', f'title={title!r} private={private}')
        locator = self._tab_button(title, private)
        if not self.driver.is_present(locator) or len(self.driver.get_elements(locator)) != 1:
            self._log_provenance(
                'INCONCLUSIVE', 'switch_tab',
                f'Tab title={title!r} private={private} was absent or non-unique in visible tray',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        self._log_provenance('ATTEMPTED', 'switch_tab', f'Selecting tab {title!r} (private={private})',
                             action_id=action_id)
        self.driver.click(xp.tab_card_for_close_button(locator))
        state = self.private_browser_state if private else self.browser_state
        self.driver.wait_until(lambda: state.validate(self.driver), 5, description='Selected Firefox tab')
        self.current_state = state
        self._log_provenance('OBSERVED', 'switch_tab', f'Switched to tab {title!r} (private={private})',
                             action_id=action_id)
        return True

    @action(tabs_state)
    def close_tab(self, title: str, private: bool = False) -> bool:
        """Close a uniquely named visible tab.

        :param title: Exact accessible title of the tab.
        :param private: Whether the tab is private.
        :return: Whether the exact tab's close control was clicked and the tab disappeared from the tray.
        """
        commit_key = (title, private)
        retry_record = self._consume_retry_commit('close_tab', commit_key)
        if retry_record is not None:
            locator = self._tab_button(title, private)
            if not self.driver.is_present(locator):
                self._log_provenance(
                    'VERIFIED', 'close_tab',
                    f'Postcondition confirmed on retry: tab {title!r} (private={private}) is no longer present; '
                    'no second click performed',
                    action_id=retry_record.action_id,
                )
                return True
            self._log_provenance(
                'AMBIGUOUS', 'close_tab',
                f'Tab {title!r} (private={private}) remained visible on retry after prior Close click; '
                'suppressing second click',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            raise FirefoxAmbiguousActionError(
                f'close_tab({title!r}, private={private}) failed after clicking Close and tab remains visible'
            ) from retry_record.error

        action_id = self._log_provenance('INTENDED', 'close_tab', f'title={title!r} private={private}')
        locator = self._tab_button(title, private)
        if not self.driver.is_present(locator):
            self._log_provenance(
                'INCONCLUSIVE', 'close_tab', f'Tab {title!r} (private={private}) not visible in tray',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        buttons = self.driver.get_elements(locator)
        if len(buttons) != 1:
            self._log_provenance(
                'AMBIGUOUS', 'close_tab',
                f'Refusing to close non-unique tab {title!r} (private={private}, matches={len(buttons)})',
                action_id=action_id, level=logging.WARNING,
            )
            return False

        record = _CommitRecord(action_id=action_id, action_name='close_tab', key=commit_key)
        self._set_pending_commit(record)
        self._log_provenance(
            'ATTEMPTED', 'close_tab', f'Closing Firefox tab {title!r} in private mode={private}',
            action_id=action_id,
        )
        try:
            buttons[0].click()
            closed = bool(
                self.driver.wait_until_not_present(locator, timeout=5, description='Closed Firefox tab disappears')
            )
            self._set_pending_commit(None)
            if closed:
                self._log_provenance(
                    'VERIFIED', 'close_tab', f'Closed tab {title!r} no longer visible in tray (UI observation)',
                    action_id=action_id,
                )
                return True
            self._log_provenance(
                'AMBIGUOUS', 'close_tab',
                f'Close button clicked for {title!r} (private={private}) but tab remained visible in tray',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        except BaseException as exc:
            record.error = exc
            raise

    @action(browser_state)
    def add_bookmark(self) -> bool:
        """Bookmark the current normal page.

        :return: Whether Firefox offered to create a new bookmark and the menu dismissed cleanly.
        """
        commit_key = ()
        retry_record = self._consume_retry_commit('add_bookmark', commit_key)
        if retry_record is not None:
            self.driver.click(xp.BROWSER_MENU)
            try:
                if self.driver.wait_until_present(xp.MENU_EDIT_BOOKMARK, xp.MENU_ADD_BOOKMARK, timeout=5):
                    if self.driver.is_present(xp.MENU_EDIT_BOOKMARK):
                        self._log_provenance(
                            'VERIFIED', 'add_bookmark',
                            'Postcondition confirmed on retry: menu now shows Edit bookmark; no second click performed',
                            action_id=retry_record.action_id,
                        )
                        return True
            finally:
                if self.driver.is_present(xp.MENU_CLOSE):
                    self.driver.back()
            self._log_provenance(
                'AMBIGUOUS', 'add_bookmark',
                'Bookmark state remained unconfirmed on retry after prior Bookmark click; suppressing duplicate click',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            raise FirefoxAmbiguousActionError(
                'add_bookmark failed after clicking Bookmark page and postcondition could not be confirmed'
            ) from retry_record.error

        action_id = self._log_provenance('INTENDED', 'add_bookmark', 'Bookmark current normal page')
        if not self.driver.is_present(xp.BROWSER_VIEW) or self.driver.is_present(xp.BROWSER_ERROR):
            self._log_provenance(
                'INCONCLUSIVE', 'add_bookmark', 'No valid WebView page active; bookmark not requested',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        self.driver.click(xp.BROWSER_MENU)
        if self.driver.wait_until_present(xp.MENU_EDIT_BOOKMARK, xp.MENU_ADD_BOOKMARK, timeout=5):
            if self.driver.is_present(xp.MENU_ADD_BOOKMARK):
                record = _CommitRecord(action_id=action_id, action_name='add_bookmark', key=commit_key)
                self._set_pending_commit(record)
                self._log_provenance(
                    'ATTEMPTED', 'add_bookmark', 'Clicking Bookmark page for current Firefox page',
                    action_id=action_id,
                )
                try:
                    self.driver.click(xp.MENU_ADD_BOOKMARK)
                    menu_closed = bool(
                        self.driver.wait_until_not_present(
                            xp.MENU_CLOSE, timeout=5, description='Firefox menu closes after bookmark request',
                        )
                    )
                    self._set_pending_commit(None)
                    if menu_closed:
                        self._log_provenance(
                            'OBSERVED', 'add_bookmark',
                            'Bookmark request submitted and menu closed (UI observation only; inspect Bookmarks to '
                            'verify visibility)',
                            action_id=action_id,
                        )
                        return True
                    self._log_provenance(
                        'AMBIGUOUS', 'add_bookmark',
                        'Bookmark page clicked but Firefox menu remained visible',
                        action_id=action_id, level=logging.WARNING,
                    )
                    return False
                except BaseException as exc:
                    record.error = exc
                    raise
        self.driver.back()
        self._log_provenance(
            'OBSERVED', 'add_bookmark', 'Bookmark page option unavailable (page may already be bookmarked)',
            action_id=action_id,
        )
        return False

    @staticmethod
    def _bookmark_item_menu(url: str) -> str:
        return xp.bookmark_item_menu(url)

    @action(bookmarks_state)
    def has_bookmark(self, url: str) -> bool:
        """Check the visible Bookmarks list for an exact stored URL without mutating bookmarks.

        :param url: Exact URL shown by Firefox, including any trailing slash.
        :return: Whether a bookmark row exposes that URL in the current viewport.
        """
        action_id = self._log_provenance('INTENDED', 'has_bookmark', f'url={url!r} read_only=True')
        present = bool(self.driver.wait_until_present(self._bookmark_item_menu(url), timeout=5))
        self._log_provenance(
            'OBSERVED', 'has_bookmark', f'url={url!r} visible_in_viewport={present}', action_id=action_id,
        )
        return present

    @action(bookmarks_state)
    def delete_bookmark(self, url: str) -> bool:
        """Delete the bookmark for an exact URL when it is unique and visible.

        :param url: Exact URL shown by Firefox, including any trailing slash.
        :return: Whether the bookmark's Delete item was clicked and the row disappeared from view.
        """
        commit_key = (url,)
        locator = self._bookmark_item_menu(url)
        retry_record = self._consume_retry_commit('delete_bookmark', commit_key)
        if retry_record is not None:
            if not self.driver.is_present(locator):
                self._log_provenance(
                    'VERIFIED', 'delete_bookmark',
                    f'Postcondition confirmed on retry: bookmark row {url!r} is no longer visible; '
                    'no second click performed',
                    action_id=retry_record.action_id,
                )
                return True
            self._log_provenance(
                'AMBIGUOUS', 'delete_bookmark',
                f'Bookmark row {url!r} remained visible on retry after Delete click; suppressing second click',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            raise FirefoxAmbiguousActionError(
                f'delete_bookmark({url!r}) failed after clicking Delete and bookmark row remains visible'
            ) from retry_record.error

        action_id = self._log_provenance('INTENDED', 'delete_bookmark', f'url={url!r}')
        if not self.driver.is_present(locator):
            self._log_provenance(
                'INCONCLUSIVE', 'delete_bookmark', f'Bookmark row {url!r} not visible in viewport',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        matches = self.driver.get_elements(locator)
        if len(matches) != 1:
            self._log_provenance(
                'AMBIGUOUS', 'delete_bookmark',
                f'Refusing to delete duplicate bookmark URL {url!r} (matches={len(matches)})',
                action_id=action_id, level=logging.WARNING,
            )
            return False

        self.driver.click(locator)
        record = _CommitRecord(action_id=action_id, action_name='delete_bookmark', key=commit_key)
        self._set_pending_commit(record)
        self._log_provenance(
            'ATTEMPTED', 'delete_bookmark', f'Clicking Delete for visible Firefox bookmark {url!r}',
            action_id=action_id,
        )
        try:
            self.driver.click(xp.BOOKMARKS_MENU_DELETE)
            removed = bool(
                self.driver.wait_until_not_present(locator, timeout=5, description='Deleted Firefox bookmark disappears')
            )
            self._set_pending_commit(None)
            if removed:
                self._log_provenance(
                    'VERIFIED', 'delete_bookmark', f'Bookmark row {url!r} removed from visible Bookmarks list',
                    action_id=action_id,
                )
                return True
            self._log_provenance(
                'AMBIGUOUS', 'delete_bookmark',
                f'Delete clicked for {url!r} but bookmark row remained visible in viewport',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        except BaseException as exc:
            record.error = exc
            raise

    @action(history_state)
    def inspect_history(self) -> list[dict[str, str]]:
        """Read visible title and URL pairs from Firefox History.

        :return: Rows from a single snapshot of the visible History list.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_history', 'read_only=True')
        rows = _read_history_rows(ElementTree.fromstring(self.driver.driver.page_source))
        self._log_provenance(
            'OBSERVED', 'inspect_history', f'visible_history_rows={len(rows)} (UI viewport snapshot only)',
            action_id=action_id,
        )
        return rows

    @action(recently_closed_state)
    def inspect_recently_closed(self) -> list[dict[str, str]]:
        """Read the visible recently closed normal tabs.

        :return: Rows from the visible Recently closed tabs list.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_recently_closed', 'read_only=True')
        rows = _read_history_rows(ElementTree.fromstring(self.driver.driver.page_source))
        self._log_provenance(
            'OBSERVED', 'inspect_recently_closed',
            f'visible_recently_closed_rows={len(rows)} (UI viewport snapshot only)',
            action_id=action_id,
        )
        return rows

    @action(browser_state)
    def save_to_new_collection(self, name: str) -> bool:
        """Save the current page to a new Firefox collection.

        :param name: Name to enter in Firefox's collection creator.
        :return: Whether Firefox displayed the collection-saved acknowledgement.
        """
        if not name.strip():
            raise ValueError('A collection name is required')
        commit_key = (name,)
        retry_record = self._consume_retry_commit('save_to_new_collection', commit_key)
        if retry_record is not None:
            self._log_provenance(
                'RETRY_SUPPRESSED', 'save_to_new_collection',
                f'Suppressing retry of save_to_new_collection({name!r}) after editor Done submission to prevent '
                'duplicate collection creation',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            raise FirefoxAmbiguousActionError(
                f'save_to_new_collection({name!r}) failed after submitting collection name; automatic retry suppressed'
            ) from retry_record.error

        action_id = self._log_provenance('INTENDED', 'save_to_new_collection', f'name={name!r}')
        if not self.driver.is_present(xp.BROWSER_VIEW) or self.driver.is_present(xp.BROWSER_ERROR):
            self._log_provenance(
                'INCONCLUSIVE', 'save_to_new_collection', 'No valid WebView page active; collection not created',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        self.driver.click(xp.BROWSER_MENU)
        self.driver.swipe_to_click_element(xp.MENU_MORE, max_swipes=4)
        self.driver.swipe_to_click_element(xp.MENU_SAVE_COLLECTION, max_swipes=4)
        self.driver.wait_until(
            lambda: self.driver.is_present(xp.COLLECTION_PICKER), 5, description='Firefox collection picker',
        )
        if not self.driver.is_present(xp.COLLECTION_NAME):
            self.driver.click(xp.COLLECTION_ADD)
        self.driver.send_keys(xp.COLLECTION_NAME, name)
        self.driver.wait_until(
            lambda: self.driver.get_element(xp.COLLECTION_NAME).get_attribute('text') == name,
            5, description='Firefox collection name',
        )

        record = _CommitRecord(action_id=action_id, action_name='save_to_new_collection', key=commit_key)
        self._set_pending_commit(record)
        self._log_provenance(
            'ATTEMPTED', 'save_to_new_collection',
            f'Submitting new collection {name!r} via editor Done action',
            action_id=action_id,
        )
        try:
            self.driver.driver.execute_script('mobile: performEditorAction', {'action': 'done'})
            saved = bool(self.driver.wait_until_present(xp.COLLECTION_SAVED, timeout=5))
            self._set_pending_commit(None)
            if saved:
                self._log_provenance(
                    'OBSERVED', 'save_to_new_collection',
                    f'Observed Collection saved toast for {name!r} (UI acknowledgement only; inspect Home to verify)',
                    action_id=action_id,
                )
                return True
            self._log_provenance(
                'AMBIGUOUS', 'save_to_new_collection',
                f'Submitted collection {name!r} but Collection saved acknowledgement was not observed; outcome '
                'ambiguous without inspecting Firefox Home',
                action_id=action_id, level=logging.WARNING,
            )
            return False
        except BaseException as exc:
            record.error = exc
            raise

    @action(browser_state)
    def has_collection(self, name: str) -> bool:
        """Look for a named collection on the currently visible normal Firefox homepage without creating a tab.

        :param name: Exact collection name.
        :return: Whether a matching name is visible on Firefox Home after a bounded scroll.
        :raises FirefoxPreconditionError: If Firefox Home (``homepage.view``) is not currently visible.
        """
        if not name.strip():
            raise ValueError('A collection name is required')
        action_id = self._log_provenance('INTENDED', 'has_collection', f'name={name!r} read_only=True')
        if not self.driver.is_present(xp.BROWSER_HOME):
            self._log_provenance(
                'INCONCLUSIVE', 'has_collection',
                f'Firefox Home (homepage.view) is not visible while inspecting collection {name!r}; '
                'refusing to create a new tab during read-only inspection',
                action_id=action_id, level=logging.WARNING,
            )
            raise FirefoxPreconditionError(
                'Firefox Home is not visible; refusing to create a new tab during read-only has_collection(). '
                'Open or switch to a Firefox Home tab explicitly before checking collections.'
            )
        locator = xp.collection_name(name)
        for _ in range(5):
            if self.driver.is_present(locator):
                self._log_provenance(
                    'OBSERVED', 'has_collection',
                    f'Collection {name!r} visible on Firefox Home (UI observation only)',
                    action_id=action_id,
                )
                return True
            scroll = self.driver.get_element(xp.BROWSER_HOME)
            if not self.driver.driver.execute_script(
                    'mobile: scrollGesture', {'elementId': scroll.id, 'direction': 'down', 'percent': 0.8}):
                break
        present = bool(self.driver.is_present(locator))
        self._log_provenance(
            'OBSERVED', 'has_collection',
            f'Collection {name!r} visible_on_home={present} after bounded scroll (UI observation only)',
            action_id=action_id,
        )
        return present

    @action(downloads_state)
    def has_download(self, filename: str) -> bool:
        """Check whether an exact filename is visible in Firefox Downloads.

        :param filename: Filename as displayed by Firefox.
        :return: Whether the filename appears in the visible Downloads list.
        """
        action_id = self._log_provenance('INTENDED', 'has_download', f'filename={filename!r} read_only=True')
        present = bool(self.driver.wait_until_present(xp.download_filename(filename), timeout=8))
        self._log_provenance(
            'OBSERVED', 'has_download',
            f'filename={filename!r} visible_in_downloads={present} (UI list entry only; does not confirm file hash)',
            action_id=action_id,
        )
        return present

    @action(browser_state)
    def download_pdf(self, url: str, expected_url: str | None = None, timeout: float = 20) -> None:
        """Open a PDF in Firefox's built-in viewer and request a download.

        :param url: Absolute HTTP(S) URL to a PDF Firefox can display inline.
        :param expected_url: Optional expected PDF URL after redirects.
        :param timeout: Maximum seconds to wait for navigation before locating the Download button.
        """
        self._check_url(url)
        if expected_url is not None:
            self._check_url(expected_url)

        commit_key = (url, expected_url, None, None)
        retry_record = self._consume_retry_commit('download_pdf', commit_key)
        if retry_record is not None:
            self._log_provenance(
                'RETRY_SUPPRESSED', 'download_pdf',
                f'Suppressing retry of download_pdf({url!r}) after commit point to prevent duplicate download',
                action_id=retry_record.action_id, level=logging.WARNING,
            )
            if isinstance(retry_record.error, FirefoxNavigationError):
                raise retry_record.error
            raise FirefoxAmbiguousActionError(
                f'download_pdf({url!r}) failed after initiating download; automatic retry suppressed'
            ) from retry_record.error

        self._submit_address(
            url,
            action_name='download_pdf',
            is_url=True,
            expected_url=expected_url,
            timeout=timeout,
            clear_commit_on_success=False,
        )
        record = self._get_pending_commit()
        action_id = record.action_id if record is not None else self._next_action_id()
        if record is None:
            record = _CommitRecord(action_id=action_id, action_name='download_pdf', key=commit_key)
            self._set_pending_commit(record)
        try:
            try:
                self.driver.wait_until(
                    lambda: self.driver.is_present(xp.BROWSER_PDF_DOWNLOAD),
                    12, description='Firefox PDF Download button',
                )
            except TimeoutError as exc:
                self._log_provenance(
                    'FAILED', 'download_pdf',
                    f'PDF viewer Download button did not appear for {url!r}',
                    action_id=action_id, level=logging.ERROR,
                )
                raise FirefoxNavigationError(
                    f'Timed out waiting for Firefox PDF viewer Download button for {url!r}'
                ) from exc

            self._log_provenance(
                'ATTEMPTED', 'download_pdf', f'Clicking Firefox PDF viewer Download button for {url!r}',
                action_id=action_id,
            )
            self.driver.click(xp.BROWSER_PDF_DOWNLOAD)
            self._set_pending_commit(None)
            self._log_provenance(
                'OBSERVED', 'download_pdf',
                f'Clicked PDF Download button for {url!r} (button click only; verify via has_download and file digest)',
                action_id=action_id,
            )
        except BaseException as exc:
            record.error = exc
            raise

    @action(settings_state)
    def inspect_settings(self) -> list[dict[str, str]]:
        """Read currently visible Firefox setting labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_settings', 'read_only=True')
        rows = self._visible_preferences()
        self._log_provenance('OBSERVED', 'inspect_settings', f'visible_rows={len(rows)}', action_id=action_id)
        return rows

    @action(site_settings_state)
    def inspect_site_settings(self) -> list[dict[str, str]]:
        """Read visible site permission labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_site_settings', 'read_only=True')
        rows = self._visible_preferences()
        self._log_provenance('OBSERVED', 'inspect_site_settings', f'visible_rows={len(rows)}', action_id=action_id)
        return rows

    @action(settings_state)
    def inspect_privacy_settings(self) -> list[dict[str, str]]:
        """Read the visible Settings rows around Enhanced Tracking Protection.

        :return: Visible labels and adjacent summary values.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_privacy_settings', 'read_only=True')
        _find_setting(self.driver, xp.SETTINGS_TRACKING)
        rows = self._visible_preferences()
        self._log_provenance('OBSERVED', 'inspect_privacy_settings', f'visible_rows={len(rows)}', action_id=action_id)
        return rows

    @action(tab_settings_state)
    def inspect_tab_settings(self) -> list[dict[str, str]]:
        """Read visible tab setting labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_tab_settings', 'read_only=True')
        rows = self._visible_preferences()
        self._log_provenance('OBSERVED', 'inspect_tab_settings', f'visible_rows={len(rows)}', action_id=action_id)
        return rows

    def _visible_preferences(self) -> list[dict[str, str]]:
        root = ElementTree.fromstring(self.driver.driver.page_source)
        result = []
        for row in root.iter():
            fields = {node.get('resource-id'): node.get('text', '') for node in row}
            title = fields.get('android:id/title') or fields.get(f'{FIREFOX_PACKAGE}:id/title')
            if title:
                summary = fields.get('android:id/summary') or fields.get(f'{FIREFOX_PACKAGE}:id/summary', '')
                result.append({'title': title, 'summary': summary})
        return result

    @action(browser_state)
    def inspect_site_info(self) -> dict[str, str]:
        """Read the current normal page's site information panel without modifying site controls.

        :return: Firefox's displayed site URL and strictly scoped tracking protection status
            (``'On'``, ``'Off'``, ``'unknown'``, or ``'ambiguous'``), or an empty dict if the panel
            button is unavailable.
        """
        action_id = self._log_provenance('INTENDED', 'inspect_site_info', 'read_only=True')
        if not self.driver.is_present(xp.BROWSER_SITE_INFO):
            self._log_provenance(
                'INCONCLUSIVE', 'inspect_site_info', 'Site information control not present on current surface',
                action_id=action_id, level=logging.WARNING,
            )
            return {}
        self.driver.click(xp.BROWSER_SITE_INFO)
        try:
            self.driver.wait_until(
                lambda: self.driver.is_present(xp.SITE_INFO_URL), 5, description='Firefox site panel',
            )
            root = ElementTree.fromstring(self.driver.driver.page_source)
            info = _parse_site_info(root)
        finally:
            if self.driver.is_present(xp.SITE_INFO_URL):
                self.driver.back()
        stage = 'OBSERVED' if info['tracking_protection'] in ('On', 'Off') else 'INCONCLUSIVE'
        level = logging.INFO if stage == 'OBSERVED' else logging.WARNING
        self._log_provenance(
            stage, 'inspect_site_info',
            f"url={info['url']!r} tracking_protection={info['tracking_protection']!r}",
            action_id=action_id, level=level,
        )
        return info
