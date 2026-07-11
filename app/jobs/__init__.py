"""Out-of-band jobs for Bug Hunter.

Each module exposes ``main()`` and runs via ``python -m app.jobs.<name>``,
invoked by an external scheduler (cron etc.), not from the request path.
"""
