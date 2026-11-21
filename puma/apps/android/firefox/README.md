# Firefox for Android

Puma supports Firefox **160.0.1** (`org.mozilla.firefox`) on Android with an **English (`en*`)** system interface. Compatibility with other Firefox versions or non-English locales is not tested and is not claimed.

Complete Firefox's first-run terms screen manually on a dedicated test profile before connecting Puma. Firefox can remain a non-default browser.

```python
from puma.apps.android.firefox.firefox import Firefox

firefox = Firefox(device_udid)
firefox.open_tab()
firefox.visit_url(
    "https://example.org",
    expected_title="Example Domain",
    verify_with=lambda: firefox.is_page_title("Example Domain"),
)
history = firefox.inspect_history()
```

## Supported actions and states

- **Navigation (`browser_state`, `private_browser_state`):**
  - `visit_url(url, expected_url=None, expected_title=None, expected_text=None, timeout=20)` and `visit_url_private(...)` require absolute HTTP(S) URLs and wait until Firefox exits the address editor, displays no browser error page, exposes a matching destination address, and satisfies any requested `expected_url`, `expected_title`, or `expected_text` constraint. Raises `FirefoxNavigationError` on error pages or timeouts rather than returning stale page content.
  - `search(query, expected_title=None, expected_text=None, timeout=20)` submits terms via the currently configured search engine. External search engines control network responses, consent prompts, and page titles.
  - `is_page_title(title, timeout=12)` and `has_page_text(text, timeout=12)` perform read-only WebView UI checks without changing tabs.
- **Tabs (`tabs_state`):**
  - `open_tab(private=False)` explicitly creates a new blank tab in normal or private mode.
  - `inspect_tabs(private=False)` reads visible tab titles in UI order without creating or closing tabs.
  - `switch_tab(title, private=False)` and `close_tab(title, private=False)` require a uniquely named visible tab. `close_tab` verifies that the closed tab's close button disappears from the tray.
- **Bookmarks (`browser_state`, `bookmarks_state`):**
  - `add_bookmark()` bookmarks the current normal page and checks that the menu dismisses.
  - `has_bookmark(url)` and `delete_bookmark(url)` match the exact URL in the visible Bookmarks viewport, including any trailing slash. Deletion requires a unique visible row and checks that the row disappears.
- **History (`history_state`, `recently_closed_state`):**
  - `inspect_history()` and `inspect_recently_closed()` return visible `{title, url}` pairs from the current UI viewport.
- **Downloads (`browser_state`, `downloads_state`):**
  - `download_pdf(url, expected_url=None, timeout=20)` navigates to an inline PDF and clicks the built-in viewer's Download button. Button activation does not confirm file completion or digest integrity; follow with `has_download(filename)` and out-of-band file/hash verification.
- **Collections (`browser_state`):**
  - `save_to_new_collection(name)` saves the current normal page to a new collection and checks for the `Collection saved` UI acknowledgement.
  - `has_collection(name)` is **strictly read-only**: it inspects the currently visible Firefox Home (`homepage.view`) with a bounded scroll and raises `FirefoxPreconditionError` if Firefox Home is not visible rather than silently creating a new tab. Call `open_tab()` explicitly when you intend to open a fresh Home tab.
- **Settings and site information (`settings_state`, `site_settings_state`, `tab_settings_state`, `browser_state`):**
  - `inspect_settings()`, `inspect_privacy_settings()`, `inspect_site_settings()`, and `inspect_tab_settings()` read visible labels and summaries without modifying toggles.
  - `inspect_site_info()` opens the site panel, extracts `url` and a strictly scoped `tracking_protection` status (`'On'`, `'Off'`, `'unknown'`, or `'ambiguous'`), and dismisses the panel in a `finally` block. It never falls back to unrelated `On`/`Off` controls elsewhere in the hierarchy.

## Forensic side effects, retries and ground-truth provenance

- **Read-only vs mutating transitions:** Returning from shared panels (`_return_to_browser`) and transitioning from `tabs_state` to `browser_state` or `private_browser_state` select an existing tab card whenever one is visible. Only when no existing tab is visible in the target tray does StateGraph recovery open a new blank tab, logged at `WARNING` level with `stage=STATE_RECOVERY_MUTATION`.
- **Non-idempotent retry safety:** PUMA's `@action` decorator automatically retries once after an exception. Mutating Firefox actions (`visit_url`, `visit_url_private`, `search`, `open_tab`, `close_tab`, `add_bookmark`, `delete_bookmark`, `save_to_new_collection`, `download_pdf`) track a local commit point:
  - Where a safe read-only postcondition exists (`add_bookmark`, `delete_bookmark`, `close_tab`), retries check whether the first attempt already took effect before refusing a second click.
  - Where repeating the interaction would create duplicate artefacts or duplicate requests (`open_tab`, `save_to_new_collection`, `download_pdf`, post-Enter navigation), automatic retry is suppressed (`stage=RETRY_SUPPRESSED`) and `FirefoxAmbiguousActionError` (or `FirefoxNavigationError`) is raised instead.
- **Provenance logging:** Firefox logs structured Ground Truth Logger (GTL) lines carrying UTC ISO-8601 timestamps (`ts=...+00:00`), sequential `action_id` values (`ff-0001`, ...), and explicit stages (`INTENDED`, `ATTEMPTED`, `OBSERVED`, `VERIFIED`, `FAILED`, `AMBIGUOUS`, `INCONCLUSIVE`, `RETRY_SUPPRESSED`, `STATE_RECOVERY_MUTATION`).
- **`verify_with` contract:** Per PUMA's `@action` framework, a failing `verify_with` predicate logs `Action '<name>' failed` to the GTL logger and **does not raise an exception**. Use `expected_url`, `expected_title`, or `expected_text` (or explicit caller assertions) when a failed check must halt execution.
- **Observation is not filesystem confirmation:** UI observations (visible history, bookmarks, downloads, collections, or absence of a private URL from visible normal history) do not confirm SQLite/profile database persistence or unrecoverability. Pair UI ground-truth logs with separate forensic acquisition when database-level claims are required.

## Running the physical-device verification script

Use synthetic data and a dedicated test profile. Keep output captures and logs outside the source checkout (enforced by the runner CLI). From the repository root, with Appium running and a dedicated English-locale Android device connected:

```powershell
.\.venv\Scripts\python.exe test_scripts/test_firefox.py --device <adb-serial> --output <output-directory>
```

By default, the runner executes only **deterministic host-controlled scenarios** using an ADB reverse tunnel to a loopback HTTP fixture (including `search_fixture` and SHA-256 verification of the downloaded PDF in `/sdcard/Download/`). To include the optional **external search-engine** check (`app.search`), pass `--include-external-search` or `--case search`.

The runner writes run metadata (UTC start time, app version, Android release/SDK, locale, and evidence-scope notice) plus scenario outcomes to `results.json`, UTC ISO-8601 GTL logs to `firefox_gtl.log`, and available UI XML/PNG captures to a new run directory. Private windows may allow UI XML while Android restricts screenshots (`FLAG_SECURE`).

See [docs/firefox-android-verification.md](../../../../docs/firefox-android-verification.md) for the StateGraph map, selector fragility inventory, offline test results, and required physical-device regression checks.
