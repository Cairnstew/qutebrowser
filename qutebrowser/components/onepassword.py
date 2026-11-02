# SPDX-FileCopyrightText: 2026 Sean Cairns <sean.cairnsst@gmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""1Password integration (native).

This is the native counterpart of the 1Password browser extension, which
cannot run inside QtWebEngine (it has neither a WebExtensions nor a native
messaging API). It talks to the 1Password CLI instead.

Usage::

    :onepass [query]

The item is picked by matching the current URL and, when given, narrowed
down by ``query`` (a title or URL substring). If several items still match,
a statusbar prompt asks for a filter.

The credentials are inserted with the page's own JavaScript context, so no
userscript or temporary file is involved.
"""

import functools
import json
import os
import shutil
import urllib.parse
from typing import Any

from qutebrowser.api import apitypes, cmdutils, message
from qutebrowser.misc import guiprocess
from qutebrowser.qt.core import QTimer
from qutebrowser.utils import usertypes, utils

OP_FALLBACK = 'op'
# NixOS installs the CLI as a setgid wrapper: the 1Password app integration
# socket is only accessible to the onepassword-cli group, and a plain `op`
# from the user profile can shadow the wrapper in GUI session PATHs.
NIXOS_WRAPPER = '/run/wrappers/bin/op'
PROMPT = '1Password'


def op_binary() -> str:
    """Resolve the 1Password CLI binary."""
    override = os.environ.get('QUTE_ONEPASS_OP')
    if override:
        return override
    if os.path.exists(NIXOS_WRAPPER):
        return NIXOS_WRAPPER
    return shutil.which(OP_FALLBACK) or OP_FALLBACK

# Kept in sync with the password_fill userscript: React/Vue controlled inputs
# need the native value setter and input/change events to register the change.
FILL_JS = """(() => {
  const u = __USERNAME__, p = __PASSWORD__;
  const visible = (el) => {
    const s = el.ownerDocument.defaultView.getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' &&
           s.opacity !== '0' && el.offsetWidth > 0 && el.offsetHeight > 0;
  };
  const set = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (d && d.set) { d.set.call(el, v); } else { el.value = v; }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
  const pw = inputs.find((i) => i.type === 'password');
  const user = inputs.find((i) =>
    i.type === 'email' || i.type === 'text' ||
    (i.getAttribute('autocomplete') || '').includes('username'));
  let n = 0;
  if (user && u) { set(user, u); n += 1; }
  if (pw && p) { set(pw, p); n += 1; }
  if (pw) { pw.focus(); }
  return n;
})()"""


def hostname(url: str) -> str:
    """Get the lowercased hostname of a URL, or an empty string."""
    try:
        return (urllib.parse.urlparse(url).hostname or '').lower()
    except ValueError:
        return ''


def host_matches(page_host: str, saved_host: str) -> bool:
    """Check whether credentials saved for ``saved_host`` apply to the page.

    A saved URL is allowed to apply to its own host or any subdomain of it
    (``example.com`` → ``mail.example.com``), never the other way around, so
    a password saved for ``mail.example.com`` is not offered on ``example.com``.
    """
    page_host = page_host.removeprefix('www.')
    saved_host = saved_host.removeprefix('www.')
    return page_host == saved_host or page_host.endswith('.' + saved_host)


def item_matches_url(item: dict[str, Any], url: str) -> bool:
    """Check whether a 1Password item was saved for the given URL."""
    page_host = hostname(url)
    if not page_host:
        return False
    for entry in item.get('urls') or []:
        saved_host = hostname(entry.get('href') or '')
        if saved_host and host_matches(page_host, saved_host):
            return True
    return False


def filter_items(items: list[dict[str, Any]], url: str,
                 query: str = '') -> list[dict[str, Any]]:
    """Get the items matching the current URL and an optional query.

    With a query, items are matched by title or saved-URL substring (the
    current URL is not required to match). Without one, only items saved for
    the current site are returned.
    """
    needle = query.strip().lower()
    if not needle:
        return [item for item in items if item_matches_url(item, url)]
    return [
        item for item in items
        if needle in (item.get('title') or '').lower()
        or any(needle in (entry.get('href') or '').lower()
               for entry in item.get('urls') or [])
    ]


def fill_js(username: str, password: str) -> str:
    """Build the JavaScript which fills the login form."""
    return (FILL_JS
            .replace('__USERNAME__', json.dumps(username))
            .replace('__PASSWORD__', json.dumps(password)))


# A command handler returns immediately, so the _OpProcess wrappers below would
# be garbage-collected (and their bound slots disconnected) before the process
# finishes. Keep them alive until the process exits.
_ACTIVE: set['_OpProcess'] = set()

# The CLI is known to have a signed-in account (checked once per session).
_accounts_ok = False

SETUP_HINT = (
    'Enable "Integrate with 1Password CLI" in the 1Password app '
    '(Settings → Developer) plus Linux system authentication '
    '(Settings → Security), or run \'op account add\' once in a terminal.')


def _no_account() -> None:
    message.error('{}: the 1Password CLI has no account configured.'.format(
        PROMPT), replace='onepassword')
    message.info(SETUP_HINT, replace='onepassword-hint')


class _OpProcess:

    """Run the 1Password CLI asynchronously.

    A watchdog kills the process if it waits for terminal input (which `op`
    does when no account is configured or a manual sign-in is required); this
    command has no way to answer such a prompt.

    Attributes:
        _handler: Called with the process' stdout on success.
        _proc: The underlying GUIProcess.
        _timer: Watchdog timer.
        _done: Whether the process already finished/timed out.
    """

    def __init__(self, args: list[str], handler: Any, *,
                 timeout_ms: int = 20000) -> None:
        self._handler = handler
        self._done = False
        self._proc = guiprocess.GUIProcess(what='1password')
        self._proc.finished.connect(self._on_finished)
        self._proc.error.connect(self._on_error)
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        _ACTIVE.add(self)
        self._timer.start(timeout_ms)
        self._proc.start(op_binary(), args)

    def _cleanup(self) -> None:
        _ACTIVE.discard(self)
        self._done = True
        self._timer.stop()

    def _on_timeout(self) -> None:
        if self._done:
            return
        self._cleanup()
        self._proc.terminate(kill=True)
        message.error('{}: op is waiting for terminal input.'.format(PROMPT),
                      replace='onepassword')
        message.info(SETUP_HINT, replace='onepassword-hint')

    def _on_finished(self, exitcode: int, _status: Any) -> None:
        if self._done:
            return
        self._cleanup()
        stderr = self._proc.stderr.strip()
        if exitcode != 0:
            if 'No accounts configured' in stderr:
                _no_account()
            else:
                message.error('{}: {}'.format(
                    PROMPT,
                    stderr or 'op exited with status {}'.format(exitcode)))
            return
        self._handler(self._proc.stdout)

    def _on_error(self, _error: Any) -> None:
        if self._done:
            return
        self._cleanup()
        message.error('{}: could not run the op CLI'.format(PROMPT))


def accounts_configured(stdout: str) -> bool:
    """Check `op account list --format json` output for a configured account."""
    try:
        accounts = json.loads(stdout or '[]')
    except ValueError:
        return False
    return isinstance(accounts, list) and bool(accounts)


def _handle_accounts(tab: apitypes.Tab, query: str, stdout: str) -> None:
    global _accounts_ok
    if not accounts_configured(stdout):
        _no_account()
        return
    _accounts_ok = True
    _list_items(tab, query)


def _list_items(tab: apitypes.Tab, query: str) -> None:
    _OpProcess(['item', 'list', '--categories', 'Login', '--format', 'json'],
               functools.partial(_handle_list, tab, query))



def _load_list(stdout: str) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(stdout or '[]')
    except ValueError:
        message.error('{}: could not parse op output'.format(PROMPT))
        return None
    if not isinstance(data, list):
        message.error('{}: unexpected op output'.format(PROMPT))
        return None
    return data


def _load_item(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        message.error('{}: could not parse op output'.format(PROMPT))
        return None
    if not isinstance(data, dict):
        message.error('{}: unexpected op output'.format(PROMPT))
        return None
    return data


def _field(item: dict[str, Any], name: str) -> str:
    for field in item.get('fields') or []:
        if field.get('id') == name or field.get('label') == name:
            return field.get('value') or ''
    return ''


def _fetch_item(item: dict[str, Any], tab: apitypes.Tab) -> None:
    item_id = item.get('id')
    if not item_id:
        message.error('{}: item without an id'.format(PROMPT))
        return
    _OpProcess(
        ['item', 'get', item_id, '--format', 'json'],
        functools.partial(_fill_item, item_id, item.get('title') or item_id, tab))


def _fill_item(item_id: str, title: str, tab: apitypes.Tab, stdout: str) -> None:
    item = _load_item(stdout)
    if item is None:
        return
    password = _field(item, 'password')
    username = _field(item, 'username')
    if not password:
        message.error('{}: no password field in "{}"'.format(PROMPT, title))
        return
    tab.run_js_async(fill_js(username, password))
    utils.set_clipboard(password)
    message.info('{}: filled "{}" — password copied to clipboard'.format(
        PROMPT, title), replace='onepassword')
    _OpProcess(['item', 'get', item_id, '--otp'],
               functools.partial(_handle_otp, title))


def _handle_otp(title: str, stdout: str) -> None:
    otp = stdout.strip()
    if not otp:
        return
    utils.set_clipboard(otp)
    message.info('{}: filled "{}" — 2FA code copied to clipboard'.format(
        PROMPT, title), replace='onepassword')


def _select(items: list[dict[str, Any]], tab: apitypes.Tab, query: str,
            *, interactive: bool = True) -> None:
    matches = filter_items(items, tab.url().toString(), query)
    if not matches:
        if query:
            message.error('{}: no item matches "{}"'.format(PROMPT, query))
        else:
            message.error('{}: no item saved for this site — '
                          'use :onepass <filter>'.format(PROMPT))
        return
    if len(matches) > 1:
        titles = ', '.join(item.get('title') or '?' for item in matches[:5])
        if len(matches) > 5:
            titles += ' (+{} more)'.format(len(matches) - 5)
        if not interactive:
            message.error('{}: still ambiguous: {}'.format(PROMPT, titles))
            return
        message.info('{}: {} matches: {}'.format(PROMPT, len(matches), titles))
        message.ask_async(
            '{} filter: '.format(PROMPT), usertypes.PromptMode.text,
            functools.partial(_select, items, tab, interactive=False))
        return
    _fetch_item(matches[0], tab)


def _handle_list(tab: apitypes.Tab, query: str, stdout: str) -> None:
    items = _load_list(stdout)
    if items is None:
        return
    _select(items, tab, query)


@cmdutils.register()
@cmdutils.argument('tab', value=cmdutils.Value.cur_tab)
def onepass(tab: apitypes.Tab, query: str = '') -> None:
    """Fill the current page's login form from 1Password.

    Args:
        query: Only consider items whose title or saved URL contains this text.
    """
    if _accounts_ok:
        _list_items(tab, query)
        return
    # Check for a usable account first: `op item list` prompts for an account
    # on the terminal when none is configured, which this command can't answer.
    _OpProcess(['account', 'list', '--format', 'json'],
               functools.partial(_handle_accounts, tab, query),
               timeout_ms=10000)
