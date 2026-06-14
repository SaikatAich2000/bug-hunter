"""Scheduled / out-of-band jobs for Bug Hunter.

These are standalone entry points meant to be run by an external scheduler
(host cron, Windows Task Scheduler, a Kubernetes CronJob, etc.) rather than
inside the request path. Each module exposes a ``main()`` and is runnable with
``python -m app.jobs.<name>``.
"""
