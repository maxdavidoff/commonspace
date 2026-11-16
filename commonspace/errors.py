class ApiError(Exception):
    """Invalid input or a rule refusal. The server turns it into HTTP 400 {"error": ...}."""

    status = 400

    def __init__(self, message, status=None):
        super().__init__(message)
        if status is not None:
            self.status = status


class NotFound(ApiError):
    status = 404
