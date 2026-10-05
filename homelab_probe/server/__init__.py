"""The web server (``hlp serve``), installed with the ``web`` extra.

Nothing outside this package imports it at module level: ``hlp serve`` imports it when it runs, so the command line
never needs FastAPI (``tests/test_server_boundary.py`` enforces both). The core stays what it is: documents in
``homelab_probe/documents.py``, accounts in ``homelab_probe/accounts.py``.
"""
