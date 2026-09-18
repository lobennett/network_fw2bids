class NetworkFW2BIDSError(Exception):
    """Base error for predictable package failures."""


class PlanningError(NetworkFW2BIDSError):
    """Flywheel contents cannot produce an unambiguous plan."""


class ConversionError(NetworkFW2BIDSError):
    """A planned archive cannot be converted safely."""
