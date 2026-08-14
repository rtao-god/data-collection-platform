from __future__ import annotations


class ReviewApplicationError(RuntimeError):
    code = "REVIEW_OPERATION_FAILED"
    owner = "ReviewApplication"
    status_code = 500

    def __init__(self, message: str, required_action: str) -> None:
        super().__init__(message)
        self.message = message
        self.required_action = required_action


class ReviewNotFound(ReviewApplicationError):
    code = "REVIEW_RESOURCE_NOT_FOUND"
    status_code = 404


class ReviewConflict(ReviewApplicationError):
    code = "REVIEW_CONFLICT"
    status_code = 409


class ReviewForbidden(ReviewApplicationError):
    code = "REVIEW_PERMISSION_DENIED"
    status_code = 403


class ReviewInputInvalid(ReviewApplicationError):
    code = "REVIEW_INPUT_INVALID"
    status_code = 422
