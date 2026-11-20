# Firefox for Android

Puma supports Firefox **160.0.1** (`org.mozilla.firefox`) with the English Android interface. Complete Firefox's first-run terms screen on a dedicated test profile before connecting Puma. Firefox can remain a non-default browser.

```python
from puma.apps.android.firefox.firefox import Firefox

firefox = Firefox(device_udid)
firefox.open_tab()
firefox.visit_url("https://example.org", verify_with=lambda: firefox.is_page_title("Example Domain"))
history = firefox.inspect_history()
```

- Use `visit_url` or `visit_url_private` for absolute HTTP(S) addresses. The return value is the accessible address bar label. Use a title or another page-specific predicate with `verify_with` when the loaded content matters.
- `search` submits terms using the current search engine. Firefox can show the query itself in the address bar. The search engine controls network responses and titles.
- Use `open_tab`, `inspect_tabs`, `switch_tab`, and `close_tab` with `private=True` for private tabs. Switching and closing require a uniquely named visible tab. Use distinct titles for synthetic tabs.
- `add_bookmark` acts on the current normal page. `has_bookmark` and `delete_bookmark` match the exact URL in the current visible bookmark list, including any trailing slash. Deletion requires a unique matching row.
- `inspect_history` and `inspect_recently_closed` return the visible title and URL pairs. They provide UI observations of the current list viewport.
- `download_pdf` uses the built-in PDF viewer's Download button. Use `has_download` to check an exact visible filename in Firefox Downloads.
- Use `save_to_new_collection` for the current normal page and `has_collection` to look for the name on Home. Use a distinct name per run.
- The settings methods read visible labels and summaries for general, privacy, site and tab settings. `inspect_site_info` reads the current page's site panel.

Use synthetic data and a dedicated test profile. Actions produce Puma ground-truth logs, including the arguments passed to them. Keep those logs and UI captures with the corresponding experiment. Private windows may allow UI XML while Android restricts screenshots.

The physical-device test uses an ADB reverse tunnel to a local HTTP fixture and an online synthetic query through the configured Google search engine. From the Puma repository, with Appium running and a dedicated Android device connected:

```powershell
.\.venv\Scripts\python.exe test_scripts/test_firefox.py --device <adb-serial> --output <output-directory>
```

The runner opens synthetic tabs, removes its exact test bookmark, closes the designated synthetic normal and private tabs, and preserves the generated history, collection and PDF for observation. It writes JSON results, a ground-truth log and available UI captures to a new run directory.

See the [capability map and verification record](../../../../docs/firefox-android-verification.md) for coverage, observations and limits.
