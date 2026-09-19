"""The negative-controls fire drill: tests/firedrill/.

Makes this directory an importable package so `_harness` and `_census` are shared
by the four drill modules (test_refusal_inventory, test_bash_refusals,
test_python_refusals, test_delivery_provenance) instead of copied into each. See
`plan.md` (dfacff61-23d5-474e-846c-2e2f0f0ea482) for the design.
"""
