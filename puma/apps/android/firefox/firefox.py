"""Puma StateGraph for Firefox on Android."""

from urllib.parse import urlsplit
from xml.etree import ElementTree

from puma.apps.android.appium_actions import supported_version
from puma.apps.android.firefox import xpaths as xp
from puma.state_graph.action import action
from puma.state_graph.popup_handler import PopUpHandler
from puma.state_graph.puma_driver import PumaDriver
from puma.state_graph.state import SimpleState, compose_clicks, scroll_to
from puma.state_graph.state_graph import StateGraph

FIREFOX_PACKAGE = 'org.mozilla.firefox'


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
        _open_normal_tab(driver)
    elif driver.is_present(xp.BROWSER_PRIVATE):
        driver.click(xp.BROWSER_TAB_COUNTER)
        _open_normal_tab(driver)


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
    tabs_state.to(browser_state, _open_normal_tab)
    tabs_state.to(private_browser_state, _open_private_tab)
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

    @action(browser_state)
    def visit_url(self, url: str) -> str:
        """Open an HTTP(S) URL in the normal browsing surface.

        :param url: Absolute HTTP or HTTPS URL.
        :return: Address as displayed by Firefox after navigation.
        """
        self._check_url(url)
        return self._submit_address(url)

    @action(private_browser_state)
    def visit_url_private(self, url: str) -> str:
        """Open an HTTP(S) URL in the private browsing surface.

        :param url: Absolute HTTP or HTTPS URL.
        :return: Address as displayed by Firefox after navigation.
        """
        self._check_url(url)
        return self._submit_address(url)

    @action(browser_state)
    def search(self, query: str) -> str:
        """Submit a query using Firefox's current default search engine.

        :param query: Search terms to type.
        :return: Address as displayed by Firefox after submission.
        """
        if not query.strip():
            raise ValueError('A search query is required')
        return self._submit_address(query)

    @staticmethod
    def _check_url(url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Firefox requires an absolute HTTP or HTTPS URL')

    def _submit_address(self, value: str) -> str:
        self.driver.click(xp.BROWSER_ADDRESS)
        self.driver.wait_until(
            lambda: self.driver.is_present(xp.BROWSER_EDIT), 5, description='Firefox address editor',
        )
        self.driver.send_keys(xp.BROWSER_EDIT, value)
        self.driver.wait_until(
            lambda: self.driver.get_element(xp.BROWSER_EDIT).get_attribute('text') == value,
            5, interval=0.25, description='Firefox address field contains the submitted text',
        )
        self.gtl_logger.info('Submitting Firefox address field')
        self.driver.press_enter()

        def loaded() -> bool:
            if self.driver.is_present(xp.POPUP_NOTIFICATIONS):
                self.driver.back()
            return (not self.driver.is_present(xp.BROWSER_EDIT)
                    and self.driver.is_present(xp.BROWSER_VIEW)
                    and self.driver.is_present(xp.BROWSER_ADDRESS))

        self.driver.wait_until(loaded, 20, interval=0.5, description='Firefox page and address bar')
        if self.driver.is_present(xp.BROWSER_ERROR):
            raise RuntimeError('Firefox displayed a browser error page')
        address = self._displayed_address()
        self.gtl_logger.info('Firefox displays address %r', address)
        return address

    def _handle_popups(self) -> None:
        if self.driver.is_present(xp.POPUP_NOTIFICATIONS):
            self.driver.back()
            return
        super()._handle_popups()

    def _displayed_address(self) -> str:
        label = self.driver.get_element(xp.BROWSER_ADDRESS).get_attribute('content-desc') or ''
        return label.strip().removesuffix('. Search or enter address')

    def is_page_title(self, title: str) -> bool:
        """Check the current page title without changing tabs.

        :param title: Exact accessible page title.
        :return: Whether Firefox exposes that title.
        """
        locator = f'{xp.BROWSER_VIEW}[@text={xp.literal(title)}]'
        return self.driver.wait_until_present(locator, timeout=12)

    @action(tabs_state)
    def open_tab(self, private: bool = False) -> None:
        """Open a blank tab in the selected browsing mode.

        :param private: Whether to open a private tab.
        """
        _open_blank_tab(self.driver, private)
        self.current_state = self.private_browser_state if private else self.browser_state

    @action(tabs_state)
    def inspect_tabs(self, private: bool = False) -> list[str]:
        """Read the visible tab titles in the selected mode.

        :param private: Whether to inspect private tabs.
        :return: Visible titles in UI order, including duplicate titles.
        """
        _select_tab_mode(self.driver, private)
        root = ElementTree.fromstring(self.driver.driver.page_source)
        return [desc.removeprefix('Close tab ') for node in root.iter('android.widget.Button')
                if (desc := node.get('content-desc', '')).startswith('Close tab ')]

    def _tab_button(self, title: str, private: bool) -> str:
        _select_tab_mode(self.driver, private)
        return f'//android.widget.Button[@content-desc={xp.literal(f"Close tab {title}")}]'

    @action(tabs_state)
    def switch_tab(self, title: str, private: bool = False) -> bool:
        """Switch to a uniquely named visible tab.

        :param title: Exact accessible title of the tab.
        :param private: Whether to select from private tabs.
        :return: Whether a unique visible tab was selected.
        """
        locator = self._tab_button(title, private)
        if not self.driver.is_present(locator) or len(self.driver.get_elements(locator)) != 1:
            return False
        self.driver.click(f"{locator}/parent::*[@clickable='true']")
        state = self.private_browser_state if private else self.browser_state
        self.driver.wait_until(lambda: state.validate(self.driver), 5, description='Selected Firefox tab')
        self.current_state = state
        return True

    @action(tabs_state)
    def close_tab(self, title: str, private: bool = False) -> bool:
        """Close a uniquely named visible tab.

        :param title: Exact accessible title of the tab.
        :param private: Whether the tab is private.
        :return: Whether the exact tab's close control was clicked.
        """
        locator = self._tab_button(title, private)
        if not self.driver.is_present(locator):
            return False
        buttons = self.driver.get_elements(locator)
        if len(buttons) != 1:
            return False
        self.gtl_logger.info('Closing Firefox tab %r in private mode=%s', title, private)
        buttons[0].click()
        return True

    @action(browser_state)
    def add_bookmark(self) -> bool:
        """Bookmark the current normal page.

        :return: Whether Firefox offered to create a new bookmark.
        """
        if not self.driver.is_present(xp.BROWSER_VIEW) or self.driver.is_present(xp.BROWSER_ERROR):
            return False
        self.driver.click(xp.BROWSER_MENU)
        if self.driver.wait_until_present(xp.MENU_EDIT_BOOKMARK, xp.MENU_ADD_BOOKMARK, timeout=5):
            if self.driver.is_present(xp.MENU_ADD_BOOKMARK):
                self.driver.click(xp.MENU_ADD_BOOKMARK)
                self.gtl_logger.info('Requested bookmark for current Firefox page')
                return True
        self.driver.back()
        return False

    @staticmethod
    def _bookmark_item_menu(url: str) -> str:
        return (f"//android.widget.TextView[@text={xp.literal(url)}]"
                "/following-sibling::android.widget.Button[starts-with(@content-desc, 'Item Menu for ')]")

    @action(bookmarks_state)
    def has_bookmark(self, url: str) -> bool:
        """Check the visible Bookmarks list for an exact stored URL.

        :param url: Exact URL shown by Firefox, including any trailing slash.
        :return: Whether a bookmark row exposes that URL.
        """
        return self.driver.wait_until_present(self._bookmark_item_menu(url), timeout=5)

    @action(bookmarks_state)
    def delete_bookmark(self, url: str) -> bool:
        """Delete the bookmark for an exact URL when it is unique and visible.

        :param url: Exact URL shown by Firefox, including any trailing slash.
        :return: Whether the bookmark's Delete item was clicked.
        """
        locator = self._bookmark_item_menu(url)
        if not self.driver.is_present(locator) or len(self.driver.get_elements(locator)) != 1:
            return False
        self.driver.click(locator)
        self.gtl_logger.info('Deleting the visible Firefox bookmark for %r', url)
        self.driver.click(xp.BOOKMARKS_MENU_DELETE)
        return True

    @action(history_state)
    def inspect_history(self) -> list[dict[str, str]]:
        """Read visible title and URL pairs from Firefox History.

        :return: Rows from a single snapshot of the visible History list.
        """
        return _read_history_rows(ElementTree.fromstring(self.driver.driver.page_source))

    @action(recently_closed_state)
    def inspect_recently_closed(self) -> list[dict[str, str]]:
        """Read the visible recently closed normal tabs.

        :return: Rows from the visible Recently closed tabs list.
        """
        return _read_history_rows(ElementTree.fromstring(self.driver.driver.page_source))

    @action(browser_state)
    def save_to_new_collection(self, name: str) -> bool:
        """Save the current page to a new Firefox collection.

        :param name: Name to enter in Firefox's collection creator.
        :return: Whether Firefox displayed the collection-saved acknowledgement.
        """
        if not name.strip():
            raise ValueError('A collection name is required')
        if not self.driver.is_present(xp.BROWSER_VIEW) or self.driver.is_present(xp.BROWSER_ERROR):
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
        self.gtl_logger.info('Saving current Firefox page to new collection %r', name)
        self.driver.driver.execute_script('mobile: performEditorAction', {'action': 'done'})
        return self.driver.wait_until_present(xp.COLLECTION_SAVED, timeout=5)

    @action(tabs_state)
    def has_collection(self, name: str) -> bool:
        """Look for a named collection on the normal Firefox homepage.

        :param name: Exact collection name.
        :return: Whether a matching name is visible after a bounded scroll.
        """
        _open_normal_tab(self.driver)
        self.current_state = self.browser_state
        locator = f"{xp.BROWSER_HOME}//android.widget.TextView[@text={xp.literal(name)}]"
        for _ in range(5):
            if self.driver.is_present(locator):
                return True
            scroll = self.driver.get_element(xp.BROWSER_HOME)
            if not self.driver.driver.execute_script(
                    'mobile: scrollGesture', {'elementId': scroll.id, 'direction': 'down', 'percent': 0.8}):
                break
        return self.driver.is_present(locator)

    @action(downloads_state)
    def has_download(self, filename: str) -> bool:
        """Check whether an exact filename is visible in Firefox Downloads.

        :param filename: Filename as displayed by Firefox.
        :return: Whether the filename appears.
        """
        return self.driver.wait_until_present(f'//android.widget.TextView[@text={xp.literal(filename)}]', timeout=8)

    @action(browser_state)
    def download_pdf(self, url: str) -> None:
        """Open a PDF in Firefox's built-in viewer and request a download.

        :param url: Absolute HTTP(S) URL to a PDF Firefox can display inline.
        """
        self._check_url(url)
        self._submit_address(url)
        self.driver.wait_until(
            lambda: self.driver.is_present(xp.BROWSER_PDF_DOWNLOAD), 12, description='Firefox PDF Download button',
        )
        self.gtl_logger.info('Requesting PDF download from the Firefox viewer')
        self.driver.click(xp.BROWSER_PDF_DOWNLOAD)

    @action(settings_state)
    def inspect_settings(self) -> list[dict[str, str]]:
        """Read currently visible Firefox setting labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        return self._visible_preferences()

    @action(site_settings_state)
    def inspect_site_settings(self) -> list[dict[str, str]]:
        """Read visible site permission labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        return self._visible_preferences()

    @action(settings_state)
    def inspect_privacy_settings(self) -> list[dict[str, str]]:
        """Read the visible Settings rows around Enhanced Tracking Protection.

        :return: Visible labels and adjacent summary values.
        """
        _find_setting(self.driver, xp.SETTINGS_TRACKING)
        return self._visible_preferences()

    @action(tab_settings_state)
    def inspect_tab_settings(self) -> list[dict[str, str]]:
        """Read visible tab setting labels and summaries.

        :return: Visible labels and adjacent summary values.
        """
        return self._visible_preferences()

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
        """Read the current normal page's site information panel.

        :return: Firefox's displayed site URL and tracking protection status, when available.
        """
        if not self.driver.is_present(xp.BROWSER_SITE_INFO):
            return {}
        self.driver.click(xp.BROWSER_SITE_INFO)
        self.driver.wait_until(lambda: self.driver.is_present(xp.SITE_INFO_URL), 5, description='Firefox site panel')
        root = ElementTree.fromstring(self.driver.driver.page_source)
        url = next((node.get('text', '') for node in root.iter()
                    if node.get('resource-id') == 'unified.trust.panel.website.url'), '')
        status = next((node.get('text', '') for node in root.iter() if node.get('text') in ('On', 'Off')), '')
        self.driver.back()
        return {'url': url, 'tracking_protection': status}
