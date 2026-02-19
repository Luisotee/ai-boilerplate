"""Tests for WhatsApp client exception hierarchy and custom attributes."""

import pytest

from ai_api.whatsapp.exceptions import (
    WhatsAppClientError,
    WhatsAppNotConnectedError,
    WhatsAppNotFoundError,
)


# ---------------------------------------------------------------------------
# WhatsAppClientError (base)
# ---------------------------------------------------------------------------


class TestWhatsAppClientError:
    def test_stores_message(self):
        exc = WhatsAppClientError("Something went wrong")
        assert exc.message == "Something went wrong"

    def test_stores_status_code(self):
        exc = WhatsAppClientError("Error", status_code=422)
        assert exc.status_code == 422

    def test_status_code_defaults_to_none(self):
        exc = WhatsAppClientError("Error")
        assert exc.status_code is None

    def test_str_representation_is_message(self):
        exc = WhatsAppClientError("Descriptive error")
        assert str(exc) == "Descriptive error"

    def test_inherits_from_exception(self):
        exc = WhatsAppClientError("Error")
        assert isinstance(exc, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(WhatsAppClientError) as exc_info:
            raise WhatsAppClientError("test error", status_code=500)
        assert exc_info.value.message == "test error"
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# WhatsAppNotConnectedError
# ---------------------------------------------------------------------------


class TestWhatsAppNotConnectedError:
    def test_default_message(self):
        exc = WhatsAppNotConnectedError()
        assert exc.message == "WhatsApp is not connected"

    def test_custom_message(self):
        exc = WhatsAppNotConnectedError("Custom disconnect message")
        assert exc.message == "Custom disconnect message"

    def test_status_code_is_503(self):
        exc = WhatsAppNotConnectedError()
        assert exc.status_code == 503

    def test_inherits_from_client_error(self):
        exc = WhatsAppNotConnectedError()
        assert isinstance(exc, WhatsAppClientError)

    def test_inherits_from_exception(self):
        exc = WhatsAppNotConnectedError()
        assert isinstance(exc, Exception)

    def test_caught_by_client_error_handler(self):
        with pytest.raises(WhatsAppClientError):
            raise WhatsAppNotConnectedError()


# ---------------------------------------------------------------------------
# WhatsAppNotFoundError
# ---------------------------------------------------------------------------


class TestWhatsAppNotFoundError:
    def test_default_message(self):
        exc = WhatsAppNotFoundError()
        assert exc.message == "Phone number not registered on WhatsApp"

    def test_custom_message(self):
        exc = WhatsAppNotFoundError("User 12345 not found")
        assert exc.message == "User 12345 not found"

    def test_status_code_is_404(self):
        exc = WhatsAppNotFoundError()
        assert exc.status_code == 404

    def test_inherits_from_client_error(self):
        exc = WhatsAppNotFoundError()
        assert isinstance(exc, WhatsAppClientError)

    def test_inherits_from_exception(self):
        exc = WhatsAppNotFoundError()
        assert isinstance(exc, Exception)

    def test_caught_by_client_error_handler(self):
        with pytest.raises(WhatsAppClientError):
            raise WhatsAppNotFoundError()

    def test_str_representation(self):
        exc = WhatsAppNotFoundError("Phone 555 not found")
        assert str(exc) == "Phone 555 not found"


# ---------------------------------------------------------------------------
# Hierarchy cross-checks
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_not_connected_is_not_not_found(self):
        exc = WhatsAppNotConnectedError()
        assert not isinstance(exc, WhatsAppNotFoundError)

    def test_not_found_is_not_not_connected(self):
        exc = WhatsAppNotFoundError()
        assert not isinstance(exc, WhatsAppNotConnectedError)

    def test_base_error_is_not_subclass_of_children(self):
        exc = WhatsAppClientError("generic")
        assert not isinstance(exc, WhatsAppNotConnectedError)
        assert not isinstance(exc, WhatsAppNotFoundError)

    def test_all_share_same_base(self):
        assert issubclass(WhatsAppNotConnectedError, WhatsAppClientError)
        assert issubclass(WhatsAppNotFoundError, WhatsAppClientError)
