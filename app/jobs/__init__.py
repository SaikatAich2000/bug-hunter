"""Out-of-band jobs for Bug Hunter.

Each module is a standalone entry point designed to be invoked by an external
scheduler (host cron, Windows Task Scheduler, Kubernetes CronJob, etc.), not
from the request path. Every module exposes a ``main()`` and can be run with
``python -m app.jobs.<name>``.
"""
