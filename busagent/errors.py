"""Domain errors.

Every error here is a *decision*, not a crash: the loop turns it into a
message the model can read, or into a workflow state a human can resume.
"""


class AgentError(Exception):
    """Base class for expected, recoverable agent failures."""


class Rejected(AgentError):
    """The gate refused a call (bad args, no permission, missing provenance)."""


class NotFound(AgentError):
    """A business record does not exist."""


class CommitTimeout(AgentError):
    """The business commit happened, but its response was lost."""


class BudgetExceeded(AgentError):
    """Turns, tool calls, or wall-clock time ran out."""
