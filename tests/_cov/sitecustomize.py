"""Start coverage inside subprocesses.

The helpers are executed as separate processes (that is how apkdiff.sh uses
them), so the parent's tracer does not see them. coverage's documented hook for
this is COVERAGE_PROCESS_START plus a startup import; PYTHONPATH pointing here
provides the import.
"""
try:
    import coverage

    coverage.process_startup()
except Exception:  # pragma: no cover - coverage absent is not an error
    pass
