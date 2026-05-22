# Enables `coverage` collection in subprocesses launched during tests.
# Activated only when COVERAGE_PROCESS_START is set (CI / coverage runs).
# See https://coverage.readthedocs.io/en/latest/subprocess.html
import os

if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage
        coverage.process_startup()
    except Exception:
        pass
