# SPDX-License-Identifier: GPL-3.0-or-later
"""Characterization tests for pi_indexer.py's public-inbox init logic.

grok-pi-indexer has no other test coverage at all, so these pin the current
behavior of the pieces init_pi_inbox() was split into before any of them get
touched again.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast

import pytest

import grokmirror
from grokmirror import pi_indexer


def make_opts(**overrides: Any) -> argparse.Namespace:
    defaults = {
        'listid_priority': None,
        'origin_host': None,
        'local_toplevel': None,
        'extra_cfgopts': None,
        'indexlevel': 'full',
        'piconfig': '/dev/null',
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


class FailingResponse:
    def raise_for_status(self) -> None:
        raise RuntimeError('boom')


class FakeRequestsSession:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requested_urls: list[str] = []

    def get(self, url: str) -> Any:
        self.requested_urls.append(url)
        return self.response


class FakeSession:
    def __init__(self, response: Any) -> None:
        self._requests = FakeRequestsSession(response)

    def get_requests_session(self) -> FakeRequestsSession:
        return self._requests


# -- parse_origins_config() --------------------------------------------------


def test_parse_origins_config_parses_basic_fields() -> None:
    origins = (
        '[publicinbox "example"]\ndescription = An example list\nnewsgroup = example.list\naddress = list@example.com'
    )
    parsed = pi_indexer.parse_origins_config(origins, 'example', [], None)
    assert parsed == ('An example list', 'example.list', None, ['list@example.com'], [])


def test_parse_origins_config_ignores_comments_and_section_header() -> None:
    origins = '; a comment\n# another comment\n[publicinbox "example"]\n\naddress = list@example.com'
    parsed = pi_indexer.parse_origins_config(origins, 'example', [], None)
    assert parsed is not None
    assert parsed[3] == ['list@example.com']


def test_parse_origins_config_computes_boost_for_matching_listid() -> None:
    origins = 'listid = foo.example.com'
    boosts = ['bar.*', 'foo.*']
    parsed = pi_indexer.parse_origins_config(origins, 'example', boosts, None)
    assert parsed is not None
    _description, _newsgroup, listid, _addresses, extraopts = parsed
    assert listid == 'foo.example.com'
    # 'foo.*' is boosts[1], so its boost value is index 1 + 10 = 11. 'listid'
    # is also in acceptopts unconditionally, so it's recorded a second time.
    assert extraopts == [('boost', '11'), ('listid', 'foo.example.com')]


def test_parse_origins_config_no_boost_without_listid_priority() -> None:
    origins = 'listid = foo.example.com'
    parsed = pi_indexer.parse_origins_config(origins, 'example', [], None)
    assert parsed is not None
    _description, _newsgroup, listid, _addresses, extraopts = parsed
    # Without a boosts list, no boost value is computed and the local
    # `listid` variable is never set -- but 'listid' is in acceptopts
    # unconditionally, so it still ends up as a plain extra opt.
    assert listid is None
    assert extraopts == [('listid', 'foo.example.com')]


def test_parse_origins_config_accepts_extra_cfgopts() -> None:
    origins = 'coderepo = example.git'
    parsed = pi_indexer.parse_origins_config(origins, 'example', [], 'coderepo,indexheaders')
    assert parsed is not None
    assert parsed[4] == [('coderepo', 'example.git')]


def test_parse_origins_config_defaults_when_missing() -> None:
    parsed = pi_indexer.parse_origins_config('', 'example', [], None)
    assert parsed == ('example archive mirror', None, None, ['example@localhost'], [])


def test_parse_origins_config_default_description_prefers_listid() -> None:
    origins = 'listid = foo.example.com'
    parsed = pi_indexer.parse_origins_config(origins, 'example', ['foo.*'], None)
    assert parsed is not None
    description, *_rest = parsed
    assert description == 'foo.example.com archive mirror'


def test_parse_origins_config_returns_none_on_malformed_line() -> None:
    origins = 'description = fine\nthis line has no equals sign'
    assert pi_indexer.parse_origins_config(origins, 'example', [], None) is None


# -- run_public_inbox_init() -------------------------------------------------


def test_run_public_inbox_init_writes_description_and_symlinks_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gdir = tmp_path / 'git' / 'example'
    (gdir / 'git').mkdir(parents=True)
    pdir = tmp_path / 'pi' / 'example'

    captured = {}

    def fake_run_shell_command(piargs: list[str], **_kwargs: object) -> tuple[int, str, str]:
        captured['piargs'] = piargs
        return 0, '', ''

    monkeypatch.setattr(pi_indexer.grokmirror, 'run_shell_command', fake_run_shell_command)

    opts = make_opts()
    result = pi_indexer.run_public_inbox_init(
        str(gdir), str(pdir), 'example', 'example', 'example.list', [('boost', '11')], ['a@b.c'], 'the desc', opts
    )

    assert result is True
    assert (pdir / 'git').is_symlink()
    assert (pdir / 'git').resolve() == (gdir / 'git').resolve()
    assert (pdir / 'description').read_text() == 'the desc'
    assert '--ng' in captured['piargs']
    assert captured['piargs'][captured['piargs'].index('--ng') + 1] == 'example.list'
    assert '-c' in captured['piargs']
    assert captured['piargs'][captured['piargs'].index('-c') + 1] == 'boost=11'


def test_run_public_inbox_init_skips_symlink_when_gdir_equals_pdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    same_dir = tmp_path / 'example'
    same_dir.mkdir()

    monkeypatch.setattr(pi_indexer.grokmirror, 'run_shell_command', lambda *_args, **_kwargs: (0, '', ''))

    opts = make_opts()
    result = pi_indexer.run_public_inbox_init(
        str(same_dir), str(same_dir), 'example', 'example', None, [], ['a@b.c'], 'the desc', opts
    )

    assert result is True
    assert not (same_dir / 'git').exists()
    assert (same_dir / 'description').read_text() == 'the desc'


def test_run_public_inbox_init_returns_false_on_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = tmp_path / 'example'
    pdir.mkdir()

    monkeypatch.setattr(pi_indexer.grokmirror, 'run_shell_command', lambda *_args, **_kwargs: (1, '', 'nope'))

    opts = make_opts()
    result = pi_indexer.run_public_inbox_init(
        str(pdir), str(pdir), 'example', 'example', None, [], ['a@b.c'], 'the desc', opts
    )

    assert result is False
    assert not (pdir / 'description').exists()


def test_run_public_inbox_init_returns_false_on_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdir = tmp_path / 'example'
    pdir.mkdir()

    def raising(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        raise OSError('no such binary')

    monkeypatch.setattr(pi_indexer.grokmirror, 'run_shell_command', raising)

    opts = make_opts()
    result = pi_indexer.run_public_inbox_init(
        str(pdir), str(pdir), 'example', 'example', None, [], ['a@b.c'], 'the desc', opts
    )

    assert result is False
    assert not (pdir / 'description').exists()


# -- init_pi_inbox() ----------------------------------------------------------


def test_init_pi_inbox_is_a_trivial_success_with_no_origins_and_no_origin_host(tmp_path: Path) -> None:
    # There are no member repos to read refs/meta/origins:i from, and no
    # --origin-host to fall back on, so init_pi_inbox() never enters its
    # 'if origins:' branch at all. That leaves the `success = True` it
    # started with untouched, so this returns True despite doing nothing.
    gdir = tmp_path / 'example'
    gdir.mkdir()
    opts = make_opts(origin_host=None)

    result = pi_indexer.init_pi_inbox(cast(grokmirror.GrokSession, FakeSession(None)), str(gdir), str(gdir), opts)

    assert result is True
    assert not (gdir / 'description').exists()


def test_init_pi_inbox_falls_back_to_http_when_no_local_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gdir = tmp_path / 'example'
    (gdir / 'git').mkdir(parents=True)
    opts = make_opts(origin_host='https://lore.example.com')

    fake_ses = FakeSession(FakeResponse('address = list@example.com'))
    monkeypatch.setattr(pi_indexer.grokmirror, 'run_shell_command', lambda *_args, **_kwargs: (0, '', ''))

    result = pi_indexer.init_pi_inbox(cast(grokmirror.GrokSession, fake_ses), str(gdir), str(gdir), opts)

    assert result is True
    assert fake_ses._requests.requested_urls == ['https://lore.example.com/example/_/text/config/raw']
    assert (gdir / 'description').read_text() == 'example archive mirror'


def test_init_pi_inbox_returns_false_when_http_fetch_fails(tmp_path: Path) -> None:
    gdir = tmp_path / 'example'
    gdir.mkdir()
    opts = make_opts(origin_host='https://lore.example.com')

    result = pi_indexer.init_pi_inbox(
        cast(grokmirror.GrokSession, FakeSession(FailingResponse())), str(gdir), str(gdir), opts
    )

    assert result is False


def test_init_pi_inbox_returns_false_on_malformed_origins(tmp_path: Path) -> None:
    gdir = tmp_path / 'example'
    gdir.mkdir()
    opts = make_opts(origin_host='https://lore.example.com')

    fake_ses = FakeSession(FakeResponse('this line has no equals sign'))
    result = pi_indexer.init_pi_inbox(cast(grokmirror.GrokSession, fake_ses), str(gdir), str(gdir), opts)

    assert result is False
    assert not (gdir / 'description').exists()
