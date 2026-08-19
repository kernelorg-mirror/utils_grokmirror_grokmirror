# Grokmirror Documentation

This directory contains the Sphinx documentation for Grokmirror.

## Building the Documentation

Install the requirements:

```bash
pip install -r requirements.txt
```

Build the HTML documentation:

```bash
make html
```

The built documentation will be available in `_build/html/index.html`.

## Documentation Structure

- `index.rst` - Overview and concepts
- `installation.rst` - Installation instructions
- `origin.rst` - Setting up an origin (primary) server
- `replica.rst` - Setting up a replica
- `configuration.rst` - Configuration reference
- `maintenance.rst` - Routine upkeep with grok-fsck and friends
- `contributing.rst` - Contributing guidelines

The per-command reference lives in the man pages under `man/`, not here.

## ReadTheDocs

This documentation is designed to be built and hosted on ReadTheDocs.

## Local Preview

To preview the documentation locally:

```bash
make html
python -m http.server --directory _build/html
```

Then open http://localhost:8000 in your browser.
