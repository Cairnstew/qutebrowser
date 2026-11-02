# SPDX-FileCopyrightText: 2026 Sean Cairns <sean.cairnsst@gmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.components.onepassword."""

import pytest

from qutebrowser.components import onepassword

ITEMS = [
    {
        'id': 'gh',
        'title': 'GitHub',
        'urls': [{'href': 'https://github.com'}],
    },
    {
        'id': 'mail',
        'title': 'Personal mail',
        'urls': [{'href': 'https://mail.example.com/login'}],
    },
    {
        'id': 'gh-enterprise',
        'title': 'GitHub Enterprise',
        'urls': [{'href': 'https://github.example.com'}],
    },
]


@pytest.mark.parametrize('url, expected', [
    ('https://github.com/login', 'github.com'),
    ('https://GitHub.com', 'github.com'),
    ('about:blank', ''),
    ('not a url', ''),
])
def test_hostname(url, expected):
    assert onepassword.hostname(url) == expected


@pytest.mark.parametrize('page_host, saved_host, expected', [
    ('github.com', 'github.com', True),
    ('www.github.com', 'github.com', True),
    ('gist.github.com', 'github.com', True),
    ('github.com', 'gist.github.com', False),
    ('github.com', 'gitlab.com', False),
    ('notgithub.com', 'github.com', False),
])
def test_host_matches(page_host, saved_host, expected):
    assert onepassword.host_matches(page_host, saved_host) is expected


@pytest.mark.parametrize('url, expected_ids', [
    ('https://github.com/login', ['gh']),
    ('https://mail.example.com/', ['mail']),
    ('https://example.com/', []),
    ('about:blank', []),
])
def test_filter_items_by_url(url, expected_ids):
    items = onepassword.filter_items(ITEMS, url)
    assert [item['id'] for item in items] == expected_ids


@pytest.mark.parametrize('query, expected_ids', [
    ('github', ['gh', 'gh-enterprise']),
    ('GITHUB', ['gh', 'gh-enterprise']),
    ('mail', ['mail']),
    ('example.com', ['mail', 'gh-enterprise']),
    ('nope', []),
])
def test_filter_items_by_query(query, expected_ids):
    # A query searches all items, even if the current URL matches nothing.
    items = onepassword.filter_items(ITEMS, 'https://nomatch.invalid/', query)
    assert [item['id'] for item in items] == expected_ids


def test_filter_items_query_narrows_url_matches():
    items = onepassword.filter_items(ITEMS, 'https://github.com/', 'enterprise')
    assert [item['id'] for item in items] == ['gh-enterprise']


def test_fill_js_escapes_credentials():
    js = onepassword.fill_js('user"quote', 'pass\\word\nnewline')
    # json.dumps produces double-quoted JS string literals.
    assert '"user\\"quote"' in js
    assert '"pass\\\\word\\nnewline"' in js


def test_fill_js_has_no_placeholders():
    js = onepassword.fill_js('user', 'pw')
    assert '__USERNAME__' not in js
    assert '__PASSWORD__' not in js
    assert 'input' in js
    assert 'password' in js


@pytest.mark.parametrize('stdout, expected', [
    ('[]', False),
    ('', False),
    ('{}', False),
    ('not json', False),
    ('[{"account_id": "ABC123"}]', True),
])
def test_accounts_configured(stdout, expected):
    assert onepassword.accounts_configured(stdout) is expected
