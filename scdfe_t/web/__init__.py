"""scdfe_t WEB entry point."""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import TransHTTPServer

__all__ = ["TransHTTPServer", "make_server", "main"]


def __getattr__(name):
    if name in __all__:
        from .server import TransHTTPServer, make_server, main
        return {"TransHTTPServer": TransHTTPServer, "make_server": make_server, "main": main}[name]
    raise AttributeError(name)
