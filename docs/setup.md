# Setup

Verified on Ubuntu 26.04.1 LTS, x86_64, WSL2, using Bash, Git 2.53.0,
uv 0.12.11, and CPython 3.12.10. The existing checkout is
`/home/chu/code/trading-bot`; no additional clone is needed.

```bash
cd /home/chu/code/trading-bot
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR=/tmp/trading-bot-uv-cache
uv --version
uv python list --only-installed
uv sync --frozen
.venv/bin/python --version
```

uv and Python 3.12 are already installed on this machine and were reused. uv finds
Python at `/home/chu/.local/share/uv/python/cpython-3.12.10-linux-x86_64-gnu/bin/python3.12`.
The system `python3` is 3.14.4; use `.venv/bin/python` or `uv run` for this project.
There is no need to change the system Python, install system pip, or activate the
virtual environment.

For a fresh Linux machine without uv, follow the
[official uv installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
If Python 3.12 is absent, run `uv python install 3.12`, then `uv sync --frozen`.
[uv detects existing Python installations](https://docs.astral.sh/uv/guides/install-python/).
The first sync downloads locked packages from PyPI. It does not contact Capital.com.
`--frozen` preserves `uv.lock`; do not regenerate it as part of setup.

The cache override makes uv usable within the restricted development workspace,
where the default home cache is read-only. It applies only to the current shell.
If uv reports a hardlink fallback across filesystems, copying is harmless; set
`export UV_LINK_MODE=copy` to silence that warning. No OS packages are needed. uv builds the editable project using setuptools in an isolated build environment; runtime dependencies remain locked.

## Safe offline validation

After syncing, run:

```bash
cd /home/chu/code/trading-bot
export UV_CACHE_DIR=/tmp/trading-bot-uv-cache
uv sync --frozen --offline --check
uv pip check --python .venv/bin/python
.venv/bin/python scripts/check_environment.py
```

The checker verifies Python 3.12 and all seven locked package versions, compiles
root, scripts, tests, and src Python files, imports dependencies and safe project modules, checks
explicit `TradingApi` constructor signatures statically, and exercises indicators and legacy
price readers with synthetic data. It runs legacy, renamed, and installed console CLI `--help` paths
with API construction, HTTP requests, network connections, dotenv loading, and
sleep blocked. The checker inspects `main.py` and `test.py` statically and exercises their help paths; the regression suite also imports `test.py` under guards, without calling its main function. No account access is needed.

You can also inspect the supported CLIs individually without starting a loop:

```bash
uv run --offline --no-sync hma.py --help
uv run --offline --no-sync multiple_hma.py --help
uv run --offline --no-sync ema_hma.py --help
uv run --offline --no-sync ping_service.py --help
```

`uv --offline` prevents uv from downloading packages; it does **not** prevent a
Python program from accessing the network. These help commands are safe because
argument parsing exits on `--help` before configuration or API construction.
Use the checker above for validation with network access blocked in Python.


## Editable installation and builds

The project now requires Python >=3.12; `.python-version` selects 3.12. Run
`uv sync --locked` after updating the checkout to install the src package and its
console commands. `uv run --locked scripts/check_offline.py` runs unittest discovery
under socket, HTTP and credential-loading guards. The old `scripts/test_offline.py`
command is an alias. `uv build` produces an sdist and wheel; the wheel includes
only the package and metadata. The sdist also includes public checkout tools,
tests, fixtures and documentation, never `.env` or integration journals.

Editable installs retain fixed checkout paths independently of the working
directory. A standalone wheel has no associated checkout: its default configuration
and journal root is `<sys.prefix>/share/trading-bot`, and missing configuration fails
closed. This does not search the shell's current directory. Operational development
commands and the environment/offline checkers require the editable checkout.
API callers can continue supplying explicit credential arguments or a credential
file to the existing loader. No CLI or implicit credential search was added.

See [configuration](configuration.md), [safety](trading-safety.md), and
[integration procedures](integration-testing.md) before contacting an account.
