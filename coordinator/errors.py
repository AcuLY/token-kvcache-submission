"""Domain errors translated to HTTP responses by :mod:`coordinator.app`."""


class CoordinatorError(Exception):
    """Base class for expected coordinator failures."""

    status_code = 400
    code = "coordinator_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConflictError(CoordinatorError):
    status_code = 409
    code = "conflict"


class NotFoundError(CoordinatorError):
    status_code = 404
    code = "not_found"


class StaleVersionError(ConflictError):
    code = "stale_version"


class AgentControlError(CoordinatorError):
    status_code = 502
    code = "agent_control_error"


class ModelServiceError(CoordinatorError):
    status_code = 502
    code = "model_service_error"


class InvalidTransitionError(ConflictError):
    code = "invalid_transition"
