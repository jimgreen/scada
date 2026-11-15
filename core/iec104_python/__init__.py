"""Pure Python IEC 60870-5-104 engine used by C/S/T. No c104/lib60870 binaries.

The project adapter API is retained; this module does not claim to implement
every API of the third-party c104 distribution.
"""
from .types import *
from .codec import encode_information, decode_information
from .api import Client, Connection, Server, Station, Point

__version__ = '1.0.0'
IMPLEMENTATION = 'python-stdlib'
