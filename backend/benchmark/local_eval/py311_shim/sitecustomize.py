# Local-only shim so a Python 3.10 interpreter can import this 3.11+ codebase.
# Not part of the repository; used only to run the test suite on this machine.
import datetime as _datetime
import enum as _enum

if not hasattr(_datetime, "UTC"):
    _datetime.UTC = _datetime.timezone.utc

if not hasattr(_enum, "StrEnum"):

    class StrEnum(str, _enum.Enum):
        def __str__(self) -> str:
            return str(self.value)

    _enum.StrEnum = StrEnum
