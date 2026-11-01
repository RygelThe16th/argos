"""Low-level ASCOM Alpaca HTTP client.

Handles GET and PUT requests to any Alpaca device endpoint,
manages ClientID / ClientTransactionID, and maps Alpaca error
numbers to typed Python exceptions.

All methods are synchronous and intended to run inside a QThread worker.
Never call them from the Qt main thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import requests

logger = logging.getLogger(__name__)

CLIENT_ID = 1
_tx_lock = threading.Lock()
_tx_counter = 0


def _next_transaction_id() -> int:
    global _tx_counter
    with _tx_lock:
        _tx_counter += 1
        return _tx_counter


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AlpacaError(Exception):
    """Alpaca device returned a non-zero ErrorNumber."""

    def __init__(self, number: int, message: str) -> None:
        super().__init__(f"Alpaca error {number}: {message}")
        self.number = number
        self.alpaca_message = message


class AlpacaTimeoutError(AlpacaError):
    """Request to the Alpaca device timed out."""

    def __init__(self, host: str, port: int) -> None:
        super().__init__(0, f"Timeout connecting to {host}:{port}")
        self.host = host
        self.port = port


class AlpacaConnectionError(AlpacaError):
    """Could not reach the Alpaca device."""

    def __init__(self, host: str, port: int, reason: str = "") -> None:
        super().__init__(0, f"Cannot connect to {host}:{port} — {reason}")
        self.host = host
        self.port = port


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AlpacaClient:
    """Synchronous ASCOM Alpaca client for a single device endpoint.

    Args:
        host: IP address or hostname of the Alpaca server.
        port: TCP port of the Alpaca server (32323 on the Seestar; 4700 is
            the separate native JSON-RPC port, not this one).
        get_timeout: Seconds before a GET request times out.
        put_timeout: Seconds before a PUT request times out.
    """

    BASE_PATH = "/api/v1"

    def __init__(
        self,
        host: str,
        port: int,
        get_timeout: float = 5.0,
        put_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.get_timeout = get_timeout
        self.put_timeout = put_timeout
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.BASE_PATH}"

    # ------------------------------------------------------------------
    # Core methods
    # ------------------------------------------------------------------

    def get(
        self,
        device_type: str,
        device_number: int,
        attribute: str,
        **extra_params: Any,
    ) -> Any:
        """Send a GET request and return the Alpaca Value field.

        Args:
            device_type: e.g. "telescope", "camera", "focuser", "filterwheel"
            device_number: Usually 0 for the Seestar.
            attribute: Alpaca property name, e.g. "rightascension".
            **extra_params: Additional query parameters.

        Returns:
            The ``Value`` field from the Alpaca JSON response.

        Raises:
            AlpacaConnectionError: Device not reachable.
            AlpacaTimeoutError: Request timed out.
            AlpacaError: Device returned a non-zero error.
        """
        url = f"{self.base_url}/{device_type}/{device_number}/{attribute}"
        params = {
            "ClientID": CLIENT_ID,
            "ClientTransactionID": _next_transaction_id(),
            **extra_params,
        }

        logger.debug("GET %s params=%s", url, params)

        try:
            response = self._session.get(url, params=params, timeout=self.get_timeout)
            response.raise_for_status()
        except requests.Timeout:
            raise AlpacaTimeoutError(self.host, self.port)
        except requests.ConnectionError as exc:
            raise AlpacaConnectionError(self.host, self.port, str(exc))
        except requests.HTTPError as exc:
            raise AlpacaConnectionError(self.host, self.port, str(exc))

        return self._parse(response)

    def put(
        self,
        device_type: str,
        device_number: int,
        method: str,
        **body_params: Any,
    ) -> Any:
        """Send a PUT request and return the Alpaca Value field (if any).

        Args:
            device_type: e.g. "telescope", "camera".
            device_number: Usually 0.
            method: Alpaca method name, e.g. "tracking".
            **body_params: Form fields to include in the request body.

        Returns:
            The ``Value`` field from the Alpaca JSON response, or None.

        Raises:
            AlpacaConnectionError: Device not reachable.
            AlpacaTimeoutError: Request timed out.
            AlpacaError: Device returned a non-zero error.
        """
        url = f"{self.base_url}/{device_type}/{device_number}/{method}"
        data = {
            "ClientID": str(CLIENT_ID),
            "ClientTransactionID": str(_next_transaction_id()),
            **{k: str(v) for k, v in body_params.items()},
        }

        logger.debug("PUT %s data=%s", url, data)

        try:
            response = self._session.put(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self.put_timeout,
            )
            response.raise_for_status()
        except requests.Timeout:
            raise AlpacaTimeoutError(self.host, self.port)
        except requests.ConnectionError as exc:
            raise AlpacaConnectionError(self.host, self.port, str(exc))
        except requests.HTTPError as exc:
            raise AlpacaConnectionError(self.host, self.port, str(exc))

        return self._parse(response)

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(response: requests.Response) -> Any:
        """Parse an Alpaca JSON response and raise on error."""
        try:
            payload = response.json()
        except Exception as exc:
            raise AlpacaError(0, f"Invalid JSON response: {exc}")

        error_number = payload.get("ErrorNumber", 0)
        error_message = payload.get("ErrorMessage", "")

        if error_number != 0:
            logger.warning("Alpaca error %d: %s", error_number, error_message)
            raise AlpacaError(error_number, error_message)

        return payload.get("Value")
