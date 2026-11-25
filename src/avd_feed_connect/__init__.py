"""Cloud PC Connect — Windows 365 / Dev Box / AVD client for Omarchy (fork of AVD Feed + Connect).

The package is split by responsibility:

  * :mod:`avd_feed_connect.config`      — constants, paths, binary discovery
  * :mod:`avd_feed_connect.http`        — thin HTTP helpers
  * :mod:`avd_feed_connect.auth`        — token storage and the OAuth2 flows
  * :mod:`avd_feed_connect.feed`        — AVD feed discovery / enumeration
  * :mod:`avd_feed_connect.rdp`         — .rdp download and sdl-freerdp launch
  * :mod:`avd_feed_connect.client`      — :class:`AvdClient`, the façade tying
                                          the pieces together
  * :mod:`avd_feed_connect.cli`         — the command-line entry point
  * :mod:`avd_feed_connect.gui`         — the GTK4 desktop application
"""

from .client import AvdClient

__all__ = ["AvdClient", "__version__"]

__version__ = "0.1.1"
