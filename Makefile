.PHONY: test perf firedrill bench check check-venv validate test-graph-up test-graph-down

# Pin the Python interpreter to the project venv. The system python3 on many
# machines lacks onnxruntime (and other optional bench dependencies), which
# silently triggers the SentenceTransformer fallback in build_pipeline() and
# turns the cold-start benchmark into a measurement of a code path the
# production daemon does not execute.
#
# Override at the command line (alternate venv layouts, CI runners with their
# own interpreter) with:  PYTHON=/path/to/python make test
PYTHON ?= .venv/bin/python3

check-venv:
	@test -x $(PYTHON) || (echo "ERROR: $(PYTHON) not found or not executable." >&2; \
	  echo "Run 'bash scripts/bootstrap.sh' (standalone) or 'bash scripts/bootstrap-plugin.sh' (plugin) to create it," >&2; \
	  echo "or set PYTHON=/path/to/python to override the default venv location." >&2; \
	  exit 1)

# The disposable Neo4j the suite runs against. scripts/test-graph.sh owns the
# container recipe, including the refusal to publish the production bolt port;
# these targets exist so nobody retypes a `docker run` line. `up` is idempotent
# (an already-serving instance skips the corpus replay but still re-applies the
# schema and re-checks that every index is ONLINE, measured at 0.68s) and it does
# nothing at all under WRIT_TEST_NO_ISOLATION=1, so the opt-out path stays a
# no-op here too.
test-graph-up:
	bash scripts/test-graph.sh up

test-graph-down:
	bash scripts/test-graph.sh down

# `test` starts the instance first so the documented entry point never fails for
# a missing container. Bare `pytest` deliberately does NOT start anything: it
# refuses at session start with a message naming `make test-graph-up`, because
# a test runner that silently creates containers on your machine is the kind of
# helpfulness that later gets blamed for something unrelated. That refusal lives
# in tests/conftest.py, so there is no guard here to drift from it.
# The failure budget for `test`, overridable from the environment. The default is
# unchanged, so `make test` with no arguments behaves exactly as HANDBOOK.md and
# docs/reference/testing.md document it. PYTEST_ADDOPTS cannot do this job: pytest
# PREPENDS its contents to the command line, so the recipe's own explicit
# --maxfail below would win over anything set there. A knob the recipe itself
# reads is the only way one run can report every failure without editing the
# recipe. pytest reads 0 as "no limit".
PYTEST_MAXFAIL ?= 10

test: check-venv test-graph-up
	# --maxfail=10, not -x. On a 7,000-test suite -x means one CI run reports exactly
	# one failure, so reaching green costs N pushes at ~8 minutes each. Ten gives the
	# whole picture in one run and still refuses to grind through a broken suite.
	$(PYTHON) -m pytest tests/ --maxfail=$(PYTEST_MAXFAIL) -q

# The timing gates, alone. `make test` deselects them (addopts in pyproject) because
# p95 inside the loaded suite measures the machine, not the hook: ~30ms of drift on
# identical code. They still have to run somewhere, and that somewhere is here, on an
# otherwise idle machine. `-p no:randomly` keeps the sample order fixed so a slow
# first query cannot land in a different position between runs.
perf: check-venv
	$(PYTHON) -m pytest -m perf -o addopts= -p no:randomly -q \
	  tests/test_hook_perf_floors.py tests/test_retrieval.py

# The negative-controls fire drill, alone. Every case triggers a real refusing
# surface (a hook subprocess, or a real call into writ/session/gates.py) and asserts
# the refusal was returned in its declared form AND recorded in the right typed
# stream. It runs in `make test` too; this target exists so a person can run it
# without remembering the marker, and so the drill's own exit status is the whole
# exit status. `-m firedrill` rather than a path so it keeps selecting the drill if
# the files move.
firedrill: check-venv
	$(PYTHON) -m pytest -m firedrill -q

bench: check-venv
	$(PYTHON) -m pytest benchmarks/bench_targets.py -x -q

validate: check-venv
	$(PYTHON) -m writ.cli validate

check: test bench validate
	@echo "All checks passed."

docs: check-venv
	$(PYTHON) scripts/render-docs.py

docs-check: check-venv
	$(PYTHON) scripts/render-docs.py --check
