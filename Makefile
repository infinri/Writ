.PHONY: test bench check check-venv validate

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
	$(PYTHON) -m pytest tests/ -x -q

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
