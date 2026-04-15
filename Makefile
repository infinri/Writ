.PHONY: test bench check

test:
	python3 -m pytest tests/ -x -q

bench:
	python3 -m pytest benchmarks/bench_targets.py -x -q

check: test bench
	@echo "All checks passed."
