# SPDX-License-Identifier: GPL-3.0-or-later
"""fetch_remote_manifest(), which gets grok-pull its view of the origin.

Existing coverage (test_integration.py, test_config_errors.py) only ever
drives this through file:// manifest URLs, or a manifest_command that fails.
That leaves the entire plain-HTTP branch untested -- the 304/error/network/
decode paths a real internet-facing origin actually exercises -- along with
manifest_command's success path and its other exit-code branches, and the
"parsed fine but empty" rejection shared by both the command and file:// paths.
"""

from __future__ import annotations

import gzip
import json

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree, http_server, write_script

# -- plain HTTP -----------------------------------------------------------------


def test_fetches_a_gzipped_manifest_over_http(tree: GrokTree) -> None:
    data = {'/test/one.git': {'fingerprint': 'abc'}}
    with gzip.open(tree.root / 'manifest.js.gz', 'wt', encoding='utf-8') as fh:
        fh.write(json.dumps(data))

    with http_server(tree.root) as base_url:
        config = tree.load_remote_config(manifest=f'{base_url}/manifest.js.gz')
        ses = grokmirror.GrokSession()

        r_manifest = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert r_manifest == data
    status = json.loads((tree.root / '.manifest.js.gz.remote').read_text())
    assert status['source'] == f'{base_url}/manifest.js.gz'
    assert status['last-fetched'] > 0


def test_fetches_a_plain_json_manifest_over_http(tree: GrokTree) -> None:
    data = {'/test/one.git': {'fingerprint': 'abc'}}
    (tree.root / 'manifest.json').write_text(json.dumps(data))

    with http_server(tree.root) as base_url:
        config = tree.load_remote_config(manifest=f'{base_url}/manifest.json')
        ses = grokmirror.GrokSession()

        r_manifest = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert r_manifest == data


def test_returns_none_when_the_server_reports_not_modified(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    data = {'/test/one.git': {'fingerprint': 'abc'}}
    (tree.root / 'manifest.json').write_text(json.dumps(data))

    with http_server(tree.root) as base_url:
        config = tree.load_remote_config(manifest=f'{base_url}/manifest.json')
        ses = grokmirror.GrokSession()

        first = grokmirror.pull.fetch_remote_manifest(ses, config)
        assert first == data

        with caplog.at_level('INFO'):
            second = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert second is None
    assert 'manifest: unchanged' in caplog.text


def test_raises_when_the_server_returns_an_error_status(tree: GrokTree) -> None:
    with http_server(tree.root) as base_url:
        config = tree.load_remote_config(manifest=f'{base_url}/does-not-exist.json')
        ses = grokmirror.GrokSession()

        with pytest.raises(grokmirror.GrokManifestError, match='404'):
            grokmirror.pull.fetch_remote_manifest(ses, config)


def test_raises_when_the_server_is_unreachable(tree: GrokTree) -> None:
    # Port 1 has nobody listening on loopback, so the connection itself fails
    # -- distinct from the error-status case above, which gets a real reply.
    config = tree.load_remote_config(manifest='http://127.0.0.1:1/manifest.json')
    ses = grokmirror.GrokSession()

    with pytest.raises(grokmirror.GrokManifestError, match='Remote server returned an error'):
        grokmirror.pull.fetch_remote_manifest(ses, config)


def test_raises_when_the_downloaded_content_is_not_valid_gzip(tree: GrokTree) -> None:
    (tree.root / 'manifest.js.gz').write_text('not actually gzip data\n')

    with http_server(tree.root) as base_url:
        config = tree.load_remote_config(manifest=f'{base_url}/manifest.js.gz')
        ses = grokmirror.GrokSession()

        with pytest.raises(grokmirror.GrokManifestError, match='Failed to parse'):
            grokmirror.pull.fetch_remote_manifest(ses, config)


# -- manifest_command -------------------------------------------------------------


def test_manifest_command_returns_the_parsed_manifest(tree: GrokTree) -> None:
    script = tree.root / 'manifest-command.sh'
    write_script(script, 'echo \'{"/test/one.git": {"fingerprint": "abc"}}\'\n')
    config = tree.load_remote_config(manifest_command=str(script))
    ses = grokmirror.GrokSession()

    r_manifest = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert r_manifest == {'/test/one.git': {'fingerprint': 'abc'}}


def test_manifest_command_exit_127_means_unchanged(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    script = tree.root / 'manifest-command.sh'
    write_script(script, 'exit 127\n')
    config = tree.load_remote_config(manifest_command=str(script))
    ses = grokmirror.GrokSession()

    with caplog.at_level('INFO'):
        r_manifest = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert r_manifest is None
    assert 'manifest: unchanged' in caplog.text


def test_manifest_command_other_nonzero_exit_is_non_fatal(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    script = tree.root / 'manifest-command.sh'
    write_script(script, 'exit 2\n')
    config = tree.load_remote_config(manifest_command=str(script))
    ses = grokmirror.GrokSession()

    with caplog.at_level('WARNING'):
        r_manifest = grokmirror.pull.fetch_remote_manifest(ses, config)

    assert r_manifest is None
    assert 'returned 2' in caplog.text


def test_manifest_command_empty_manifest_is_rejected(tree: GrokTree) -> None:
    script = tree.root / 'manifest-command.sh'
    write_script(script, "echo '{}'\n")
    config = tree.load_remote_config(manifest_command=str(script))
    ses = grokmirror.GrokSession()

    with pytest.raises(grokmirror.GrokManifestError, match='Empty manifest'):
        grokmirror.pull.fetch_remote_manifest(ses, config)


# -- file:// ------------------------------------------------------------------


def test_file_url_with_an_empty_manifest_is_rejected(tree: GrokTree) -> None:
    remote_manifest = tree.root / 'remote-manifest.json'
    remote_manifest.write_text('{}')
    config = tree.load_remote_config(manifest=remote_manifest)
    ses = grokmirror.GrokSession()

    with pytest.raises(grokmirror.GrokManifestError, match='Empty manifest'):
        grokmirror.pull.fetch_remote_manifest(ses, config)
