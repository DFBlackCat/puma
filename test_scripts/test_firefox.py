"""Physical-device Firefox verification using a run-specific synthetic corpus."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

from selenium.common.exceptions import WebDriverException

from puma.apps.android.firefox.firefox import Firefox
from puma.apps.android.firefox import xpaths as xp
from puma.state_graph.state import State


def _pdf(label: str) -> bytes:
    stream = f'BT /F1 18 Tf 60 700 Td ({label}) Tj ET'.encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> >> >>',
        b'<< /Length %d >>\nstream\n%s\nendstream' % (len(stream), stream),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    data = bytearray(b'%PDF-1.4\n')
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(b'%d 0 obj\n%s\nendobj\n' % (index, obj))
    xref = len(data)
    data.extend(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1))
    for offset in offsets:
        data.extend(b'%010d 00000 n \n' % offset)
    data.extend(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref))
    return bytes(data)


def _adb(device: str, *arguments: str) -> str:
    result = subprocess.run(['adb', '-s', device, *arguments], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


@contextmanager
def _fixture(device: str, run_id: str):
    filename = f'PUMA-Firefox-{run_id}-06.pdf'
    pdf = _pdf(f'PUMA Firefox {run_id} 06 controlled PDF')
    titles = {key: f'PUMA {run_id} {key}' for key in ('01-visit', '03-bookmark', '04-tab', '05-private')}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == f'/{run_id}/{filename}':
                content_type, body = 'application/pdf', pdf
            elif path.startswith(f'/{run_id}/') and (key := path.rsplit('/', 1)[-1]) in titles:
                title = titles[key]
                content_type = 'text/html; charset=utf-8'
                body = f'<!doctype html><html><title>{title}</title><h1>{title}</h1></html>'.encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        port = f'tcp:{server.server_port}'
        _adb(device, 'reverse', '--no-rebind', port, port)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}/{run_id}'
            yield base, titles, filename, sha256(pdf).hexdigest()
        finally:
            server.shutdown()
            worker.join()
            _adb(device, 'reverse', '--remove', port)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _capture(app: Firefox, output: Path, name: str) -> None:
    (output / f'{name}.xml').write_text(app.driver.driver.page_source, encoding='utf-8')
    try:
        app.driver.driver.save_screenshot(str(output / f'{name}.png'))
    except WebDriverException as error:
        if "'secure' flag" not in error.msg:
            raise
        app.gtl_logger.info('Android restricted the screenshot for %s. The UI XML was retained.', name)


def _run(app: Firefox, device: str, run_id: str, output: Path, selected: list[str] | None) -> dict:
    results = {}
    with _fixture(device, run_id) as (base, titles, filename, digest):
        visit_url = f'{base}/01-visit'
        bookmark_url = f'{base}/03-bookmark'
        tab_url = f'{base}/04-tab'
        private_url = f'{base}/05-private'
        app.open_tab()

        def visit() -> str:
            address = app.visit_url(visit_url, verify_with=lambda: app.is_page_title(titles['01-visit']))
            _require(app.is_page_title(titles['01-visit']), 'Controlled title did not load')
            _require(visit_url.removeprefix('http://') in address, f'Unexpected address {address!r}')
            _capture(app, output, '01_browser')
            return address

        def search() -> str:
            query = f'PUMA Firefox {run_id} 02 synthetic search'
            address = app.search(query)
            expected_title = f'{query} - Google Search'
            _require(app.is_page_title(expected_title), 'Google did not show the exact synthetic query in its title')
            _capture(app, output, '02_search')
            return f'{address} ({expected_title})'

        def bookmark() -> str:
            app.visit_url(bookmark_url)
            _require(app.is_page_title(titles['03-bookmark']), 'Bookmark target did not load')
            _require(app.add_bookmark(), 'Firefox did not offer Add bookmark')
            _require(app.has_bookmark(bookmark_url), 'Exact synthetic bookmark did not appear')
            _capture(app, output, '03_bookmarks')
            _require(app.delete_bookmark(bookmark_url), 'Exact synthetic bookmark did not offer Delete')
            _require(not app.has_bookmark(bookmark_url), 'Synthetic bookmark remained visible')
            return 'Created, observed and removed the exact synthetic URL'

        def tabs() -> str:
            app.open_tab()
            app.visit_url(tab_url)
            _require(app.is_page_title(titles['04-tab']), 'Normal tab target did not load')
            app.open_tab()
            app.visit_url(visit_url)
            _require(titles['04-tab'] in app.inspect_tabs(), 'Synthetic normal tab was absent')
            _capture(app, output, '04_tabs')
            _require(app.switch_tab(titles['04-tab']), 'Synthetic normal tab was not uniquely visible')
            _require(app.is_page_title(titles['04-tab']), 'Selected the wrong page')
            _require(app.close_tab(titles['04-tab']), 'Could not close the synthetic normal tab')
            _require(titles['04-tab'] not in app.inspect_tabs(), 'Closed normal tab was still visible')
            rows = app.inspect_recently_closed()
            _capture(app, output, '04_recently_closed')
            _require(any(row['title'] == titles['04-tab'] for row in rows), 'Closed tab did not appear in inspection')
            return 'Opened, switched and closed the normal tab and observed it in Recently closed tabs'

        def private() -> str:
            app.open_tab(private=True)
            address = app.visit_url_private(private_url)
            _require(private_url.removeprefix('http://') in address, f'Unexpected private address {address!r}')
            _require(app.is_page_title(titles['05-private']), 'Private tab target did not load')
            _capture(app, output, '05_private_browser')
            _require(titles['05-private'] in app.inspect_tabs(private=True), 'Private title missing in private tray')
            _capture(app, output, '05_private_tabs')
            _require(app.switch_tab(titles['05-private'], private=True), 'Could not switch to the private test tab')
            _require(app.close_tab(titles['05-private'], private=True), 'Could not close the private test tab')
            _require(titles['05-private'] not in app.inspect_tabs(private=True), 'Closed private tab still visible')
            rows = app.inspect_history()
            _require(all(row['url'] != private_url for row in rows), 'Private URL was visible in normal History')
            return 'Opened, switched and closed a private tab. Its URL was absent from visible normal History.'

        def download() -> dict:
            app.download_pdf(f'{base}/{filename}')
            _capture(app, output, '06_download_requested')
            _require(app.has_download(filename), 'Exact PDF filename was absent from Firefox Downloads')
            _capture(app, output, '06_downloads')
            shared_path = f'/sdcard/Download/{filename}'
            observed = _adb(device, 'shell', 'sha256sum', shared_path).split()[0]
            _require(observed == digest, 'Shared download bytes did not match the fixture')
            return {'filename': filename, 'expected_sha256': digest, 'observed_sha256': observed}

        def history() -> str:
            app.visit_url(visit_url)
            _require(app.is_page_title(titles['01-visit']), 'History target did not load')
            rows = app.inspect_history()
            _capture(app, output, '07_history')
            _require(any(row['url'] == visit_url and row['title'] == titles['01-visit'] for row in rows),
                     'Exact visit title and URL were absent from visible History')
            return 'Matching title and URL observed in normal History'

        def settings() -> str:
            normal = app.inspect_settings()
            _capture(app, output, '08_settings')
            privacy = app.inspect_privacy_settings()
            _capture(app, output, '08_privacy_settings')
            site = app.inspect_site_settings()
            _capture(app, output, '08_site_settings')
            tab = app.inspect_tab_settings()
            _capture(app, output, '08_tab_settings')
            _require(any(row['title'] == 'Search' for row in normal), 'Firefox Settings rows were absent')
            _require(any(row['title'] == 'Enhanced Tracking Protection' for row in privacy),
                     'Tracking Protection settings were absent')
            _require(any(row['title'] == 'Autoplay' for row in site), 'Site permissions were absent')
            _require(any(row['title'] == 'List' for row in tab), 'Tab layout settings were absent')
            app.visit_url(visit_url)
            info = app.inspect_site_info()
            _require('127.0.0.1' in info.get('url', ''), f'Wrong site panel {info!r}')
            return 'Read settings, site permissions, tab layout and the current site panel'

        def collection() -> str:
            name = f'PUMA Firefox {run_id} 09 collection'
            app.visit_url(visit_url)
            _require(app.is_page_title(titles['01-visit']), 'Collection target did not load')
            saved = app.save_to_new_collection(name)
            _capture(app, output, '09_collection_save')
            _require(app.has_collection(name), f'Collection absent from Home (save acknowledgement={saved})')
            _capture(app, output, '09_collection_home')
            return f'Saved the page and observed the collection {name!r} on the homepage'

        def recovery() -> str:
            app.go_to_state(Firefox.tabs_state)
            tray_overlays = (
                ('tab_menu', xp.TABS_MENU, Firefox.tabs_menu_state),
                ('groups', "//*[@clickable='true' and contains(@content-desc, 'tab groups open.')]",
                 Firefox.groups_state),
                ('sync', "//*[@clickable='true' and starts-with(@content-desc, 'Synchronised Tabs Open:')]",
                 Firefox.synced_tabs_state),
            )
            for name, entry, state in tray_overlays:
                app.driver.click(entry)
                app.driver.wait_until(lambda: state.validate(app.driver), 5)
                _capture(app, output, f'10_{name}')
                app.go_to_state(Firefox.tabs_state)
                _require(Firefox.tabs_state.validate(app.driver), f'Did not recover from {name}')
            app.go_to_state(Firefox.browser_state)
            app.driver.click(xp.BROWSER_ADDRESS)
            app.driver.wait_until(lambda: Firefox.address_edit_state.validate(app.driver), 5)
            _capture(app, output, '10_address_edit')
            app.go_to_state(Firefox.browser_state)
            _require(Firefox.browser_state.validate(app.driver), 'Did not recover from the address editor')
            return 'Recovered from the tab menu, groups, synced tabs and the address editor'

        def panel_recovery() -> str:
            def return_from(name: str, panel: State, destination: State) -> None:
                app.driver.wait_until(lambda: panel.validate(app.driver), 5, description=f'Firefox {name}')
                _capture(app, output, f'11_{name}')
                app.go_to_state(destination)
                _require(destination.validate(app.driver), f'Did not recover from {name}')

            app.go_to_state(Firefox.private_browser_state)
            app.go_to_state(Firefox.menu_state)
            return_from('private_menu', Firefox.menu_state, Firefox.browser_state)

            app.visit_url(bookmark_url)
            _require(app.is_page_title(titles['03-bookmark']), 'Panel test page did not load')
            app.driver.click(xp.BROWSER_SITE_INFO)
            return_from('site_info', Firefox.site_info_state, Firefox.browser_state)

            app.driver.click(xp.BROWSER_MENU)
            app.driver.swipe_to_click_element(xp.MENU_MORE, max_swipes=4)
            app.driver.swipe_to_click_element(xp.MENU_SAVE_COLLECTION, max_swipes=4)
            return_from('collection_picker', Firefox.collection_picker_state, Firefox.browser_state)

            _require(app.add_bookmark(), 'Synthetic panel test bookmark was not created')
            _require(app.has_bookmark(bookmark_url), 'Synthetic panel test bookmark was not visible')
            app.driver.click(app._bookmark_item_menu(bookmark_url))
            return_from('bookmark_menu', Firefox.bookmark_menu_state, Firefox.bookmarks_state)
            _require(app.delete_bookmark(bookmark_url), 'Could not remove the synthetic panel test bookmark')
            _require(not app.has_bookmark(bookmark_url), 'Synthetic panel test bookmark remained visible')

            app.go_to_state(Firefox.settings_state)
            delete_data_row = "//*[@resource-id='android:id/title' and @text='Delete browsing data']"
            app.driver.swipe_to_click_element(delete_data_row, max_swipes=5)
            return_from('delete_data_view_only', Firefox.delete_data_state, Firefox.settings_state)
            return 'Recovered from the private menu, site panel, collection picker, bookmark menu and data panel'

        cases = {'visit': visit, 'search': search, 'bookmark': bookmark, 'tabs': tabs,
                 'private': private, 'download': download, 'history': history, 'settings': settings,
                 'collection': collection, 'recovery': recovery, 'panel_recovery': panel_recovery}
        for name, scenario in cases.items():
            if selected and name not in selected:
                continue
            try:
                results[name] = {'status': 'passed', 'observed': scenario()}
            except Exception as error:
                results[name] = {'status': 'failed', 'error': str(error).replace(device, '<device>')}
                try:
                    _capture(app, output, f'failed_{name}')
                except Exception as capture_error:
                    results[name]['capture_error'] = str(capture_error).replace(device, '<device>')
            print(f'{name}: {results[name]}', flush=True)
            (output / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    return results


def main() -> int:
    """Run the selected live scenarios and return 1 if any scenario fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', required=True, help='ADB serial for the dedicated test phone')
    parser.add_argument('--appium-server', default='http://127.0.0.1:4723')
    parser.add_argument('--output', type=Path, required=True, help='Directory for UI captures and results')
    parser.add_argument('--case', action='append',
                        choices=('visit', 'search', 'bookmark', 'tabs', 'private', 'download', 'history', 'settings',
                                 'collection', 'recovery', 'panel_recovery'))
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repository):
        parser.error('Store device captures and logs outside the source checkout')
    locale = _adb(args.device, 'shell', 'getprop', 'persist.sys.locale')
    if not locale.startswith('en'):
        parser.error(f'Expected an English Android locale, found {locale!r}')
    run_id = datetime.now(timezone.utc).strftime('%m%d%H%M') + '-' + uuid4().hex[:4]
    output = args.output.resolve() / run_id
    output.mkdir(parents=True)
    print(f'Firefox verification output: {output}', flush=True)
    original_timeout = _adb(args.device, 'shell', 'settings', 'get', 'system', 'screen_off_timeout')
    change_timeout = original_timeout.isdigit() and int(original_timeout) < 1800000
    app = None
    try:
        if change_timeout:
            _adb(args.device, 'shell', 'settings', 'put', 'system', 'screen_off_timeout', '1800000')
        app = Firefox(args.device, appium_server=args.appium_server)
        version = app.driver.get_app_version()
        _require(version == Firefox.supported_version,
                 f'Expected Firefox {Firefox.supported_version}, found {version}')
        handler = logging.FileHandler(output / 'firefox_gtl.log', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        app.gtl_logger.addHandler(handler)
        try:
            results = _run(app, args.device, run_id, output, args.case)
        finally:
            app.gtl_logger.removeHandler(handler)
            handler.close()
        return int(any(result['status'] == 'failed' for result in results.values()))
    finally:
        try:
            if app:
                app.driver.driver.quit()
        finally:
            if change_timeout:
                _adb(args.device, 'shell', 'settings', 'put', 'system', 'screen_off_timeout', original_timeout)
            _adb(args.device, 'shell', 'input', 'keyevent', 'KEYCODE_HOME')


if __name__ == '__main__':
    raise SystemExit(main())
