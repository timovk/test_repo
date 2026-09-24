"""Domain exceptions.  Every error raised by application code derives from :class:`NLFedError`."""

from __future__ import annotations


class NLFedError(Exception):
    """Base class for all application errors."""


class ConfigError(NLFedError):
    """Invalid or missing configuration."""


class DataNotPreparedError(NLFedError):
    """Required geographic data has not been downloaded/built yet."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "Geographic data not prepared. Run `python -m app geography download` "
            "and `python -m app geography build` first."
        )


class DownloadError(NLFedError):
    """A source dataset could not be downloaded."""


class ValidationError(NLFedError):
    """A constitutional or data-integrity invariant is violated."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        self.problems = problems or []
        detail = ("\n  - " + "\n  - ".join(self.problems)) if self.problems else ""
        super().__init__(message + detail)


class ApportionmentError(NLFedError):
    """Apportionment could not be computed."""


class DistrictingError(NLFedError):
    """District generation failed or produced an invalid plan."""


class ElectionError(NLFedError):
    """Invalid election / race / ballot state."""


class NotFoundError(NLFedError):
    """A requested entity does not exist."""


class ScenarioError(NLFedError):
    """Invalid scenario document."""


class ElectionNightError(NLFedError):
    """Invalid election-night state transition."""
