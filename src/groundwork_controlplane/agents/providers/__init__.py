"""Provider seam: the only place a real Foundry (or future model host) client is constructed.

:mod:`groundwork_controlplane.agents.planning` never imports a provider SDK directly — it asks
this package for a client and calls the OpenAI-compatible chat-completions shape on whatever
comes back. Swapping the underlying provider or client library is a change here, not in
``planning.py``.
"""

from __future__ import annotations
