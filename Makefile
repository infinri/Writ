.PHONY: test perf bench check check-venv validate

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

test: check-venv
	# --maxfail=10, not -x. On a 7,000-test suite -x means one CI run reports exactly
	# one failure, so reaching green costs N pushes at ~8 minutes each. Ten gives the
	# whole picture in one run and still refuses to grind through a broken suite.
	$(PYTHON) -m pytest tests/ --maxfail=10 -q

# The timing gates, alone. `make test` deselects them (addopts in pyproject) because
# p95 inside the loaded suite measures the machine, not the hook: ~30ms of drift on
# identical code. They still have to run somewhere, and that somewhere is here, on an
# otherwise idle machine. `-p no:randomly` keeps the sample order fixed so a slow
# first query cannot land in a different position between runs.
perf: check-venv
	$(PYTHON) -m pytest -m perf -o addopts= -p no:randomly -q \
	  tests/test_hook_perf_floors.py tests/test_retrieval.py

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
