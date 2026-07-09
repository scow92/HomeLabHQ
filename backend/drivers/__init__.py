"""Driver package. Importing it self-registers every bundled driver."""
from . import generic_ssh   # noqa: F401
from . import generic_snmp  # noqa: F401
from . import generic_api   # noqa: F401
from . import generic_http  # noqa: F401
from . import keeplink      # noqa: F401
from . import openwrt       # noqa: F401
from . import mikrotik      # noqa: F401
from . import snmp_switch   # noqa: F401
