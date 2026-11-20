"""Firefox Android accessibility locators."""


def _id(value: str) -> str:
    return f"//*[@resource-id='{value}']"


def _firefox_id(value: str) -> str:
    return _id(f'org.mozilla.firefox:id/{value}')


def _desc(value: str) -> str:
    return f"//*[@content-desc='{value}']"


def _text(value: str) -> str:
    return f"//*[@text='{value}']"


def literal(value: str) -> str:
    """Quote a caller-supplied XPath string.

    :param value: The exact UI string to match.
    :return: An XPath string expression.
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = [f"'{part}'" for part in value.split("'")]
    return "concat(" + ', "\'", '.join(parts) + ")"


BROWSER_ADDRESS = _id('ADDRESSBAR_URL_BOX')
BROWSER_EDIT = _id('ADDRESSBAR_SEARCH_BOX')
BROWSER_TAB_COUNTER = _id('ADDRESSBAR_TABS_COUNTER')
BROWSER_HOME = _id('homepage.view')
BROWSER_PRIVATE = f"{BROWSER_TAB_COUNTER}//*[starts-with(@content-desc, 'Private Tabs Open:')]"
BROWSER_NORMAL = f"{BROWSER_TAB_COUNTER}//*[starts-with(@content-desc, 'Non-private Tabs Open:')]"
BROWSER_VIEW = f"{_firefox_id('engineView')}//android.webkit.WebView"
BROWSER_ERROR = _id('errorPageContainer')
BROWSER_MENU = _desc('More options')
BROWSER_SITE_INFO = _desc('Site information')
BROWSER_PDF_DOWNLOAD = f"{BROWSER_VIEW}//android.widget.Button[@resource-id='download' and @text='Download']"

MENU_CLOSE = _desc('Close menu')
MENU_HISTORY = _desc('History')
MENU_BOOKMARKS = _desc('Bookmarks')
MENU_DOWNLOADS = _desc('Downloads')
MENU_SETTINGS = _desc('Settings')
MENU_ADD_BOOKMARK = _desc('Bookmark page')
MENU_EDIT_BOOKMARK = _desc('Edit bookmark')
MENU_MORE = _desc('More Collapsed')
MENU_SAVE_COLLECTION = _desc('Save to collection')

TABS_MENU = _desc('Open tabs menu')
TABS_ADD_NORMAL = _desc('Add tab')
TABS_ADD_PRIVATE = _desc('Add private tab')
TABS_SELECT_NORMAL = "//*[@clickable='true' and ./*[starts-with(@content-desc, 'Normal Tabs Open:')]]"
TABS_SELECT_PRIVATE = "//*[@clickable='true' and starts-with(@content-desc, 'Private Tabs Open:')]"
TABS_GROUPS_SELECTED = "//*[@selected='true' and contains(@content-desc, 'tab groups open.')]"
TABS_SYNCED_SELECTED = "//*[@selected='true' and starts-with(@content-desc, 'Synchronised Tabs Open:')]"
TABS_MENU_RECENT = _text('Recently closed tabs')
TABS_MENU_SETTINGS = _text('Tab settings')

BOOKMARKS_TOOLBAR = _id('bookmarks.toolbar')
BOOKMARKS_MENU_PRIVATE = _text('Open in private tab')
BOOKMARKS_MENU_DELETE = _text('Delete')

HISTORY_LAYOUT = _firefox_id('historyLayout')
HISTORY_LIST = _firefox_id('history_list')
HISTORY_RECENTLY_CLOSED = _firefox_id('recently_closed_tabs_header')
RECENTLY_CLOSED_LAYOUT = _firefox_id('recentlyClosedLayout')
DOWNLOADS_SETTINGS = _desc('Navigate to Downloads Settings')
COLLECTION_PICKER = _firefox_id('createCollectionWrapper')
COLLECTION_NAME = _firefox_id('name_collection_edittext')
COLLECTION_ADD = _text('Add new collection')
COLLECTION_SAVED = _text('Collection saved')
SITE_INFO_URL = _id('unified.trust.panel.website.url')

SETTINGS_SEARCH = _desc('Settings search button')
SETTINGS_SITE = "//*[@resource-id='android:id/title' and @text='Site settings']"
SETTINGS_TAB = "//*[@resource-id='android:id/title' and @text='Tabs']"
SETTINGS_TRACKING = "//*[@resource-id='android:id/title' and @text='Enhanced Tracking Protection']"
SETTINGS_LIST = _firefox_id('recycler_view')
_NAVIGATION_TOOLBAR = _firefox_id('navigationToolbar')
SITE_SETTINGS_TITLE = f"{_NAVIGATION_TOOLBAR}//*[@text='Site settings']"
TAB_SETTINGS_TITLE = f"{_NAVIGATION_TOOLBAR}//*[@text='Tabs']"
DELETE_DATA_TITLE = f"{_NAVIGATION_TOOLBAR}//*[@text='Delete browsing data']"

POPUP_DEFAULT_TITLE = _text('Set Firefox as your default browser app?')
POPUP_DEFAULT_CANCEL = "//*[@resource-id='android:id/button2' and @text='Cancel']"
POPUP_DEFAULT_OFFER = _text('Open all your links with built-in privacy')
POPUP_DEFAULT_SKIP = _text('Not now')
POPUP_NOTIFICATIONS = _text('Allow Firefox to send you notifications?')
