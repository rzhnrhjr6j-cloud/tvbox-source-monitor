# tvbox-source-monitor - one-command local workflow (spec §36)
#
#   make install    create .venv and install dependencies
#   make discover   find new candidate sources
#   make check      probe every known source (L1-L5)
#   make build      score, retire, revive and regenerate dist/tvbox.json
#   make verify-android  load every csp_* jar on a real device/emulator
#   make report     print the current state
#   make test       run the test suite
#   make pipeline   the whole chain in one shot (spec §36)
#
# Extra helpers: doctor, bootstrap, demo, serve-demo, clean

PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin
ifeq ($(wildcard $(BIN)/python),)
PY := $(PYTHON)
else
PY := $(BIN)/python
endif

ARGS ?=

.PHONY: help install discover check build report test pipeline doctor bootstrap demo serve-demo verify-android clean fmt

help:
	@sed -n '2,12p' Makefile | sed 's/^# \?//'

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/python -m pip install -r requirements.txt
	$(BIN)/python -m pip install "pytest>=8.0" "pytest-cov>=5.0"
	@echo "installed into $(VENV)"

discover:
	$(PY) -m app.main discover $(ARGS)

check:
	$(PY) -m app.main check $(ARGS)

build:
	$(PY) -m app.main build $(ARGS)

verify-android:
	$(PY) tools/android_verify.py $(ARGS)

report:
	$(PY) -m app.main report $(ARGS)

test:
	$(PY) -m pytest -q

pipeline:
	$(PY) -m app.main pipeline $(ARGS)

doctor:
	$(PY) -m app.main doctor $(ARGS)

bootstrap:
	$(PY) -m app.main bootstrap $(ARGS)

# spec §33: drives the ten acceptance demos against a real local origin
demo:
	$(PY) scripts/demo.py

# same origin, left running so you can poke at it by hand
serve-demo:
	$(PY) scripts/demo.py --serve-only

clean:
	$(PY) -c "import shutil,pathlib; [shutil.rmtree(p, ignore_errors=True) for p in ('.pytest_cache','app/__pycache__')]"
	$(PY) -c "import pathlib; [f.unlink() for f in pathlib.Path('.').rglob('*.pyc')]"
