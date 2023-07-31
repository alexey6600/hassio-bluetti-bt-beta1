"""Coordinator for Bluetti integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import cast
import async_timeout

from bleak import BleakClient, BleakError

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
)

from bluetti_mqtt.bluetooth.client import BluetoothClient
from bluetti_mqtt.bluetooth import (
    BadConnectionError,
    ModbusError,
    ParseError,
    build_device,
)

_LOGGER = logging.getLogger(__name__)


class PollingCoordinator(DataUpdateCoordinator):
    """Polling coordinator."""

    def __init__(self, hass: HomeAssistant, address, device_name: str):
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Bluetti polling coordinator",
            update_interval=timedelta(seconds=20),
        )
        self._address = address
        self.notify_future = None
        self.current_command = None
        self.notify_response = bytearray()
        self.bluetti_device = build_device(address, device_name)

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        self.logger.debug("Polling data")

        device = bluetooth.async_ble_device_from_address(self.hass, self._address)
        if device is None:
            self.logger.error("Device not available")
            return None

        if self.bluetti_device is None:
            self.logger.error("Device type not found")
            return None

        # Polling
        client = BleakClient(device)
        parsed_data: dict = {}

        try:
            async with async_timeout.timeout(15):
                await client.connect()

                await client.start_notify(
                    BluetoothClient.NOTIFY_UUID, self._notification_handler
                )

                for command in self.bluetti_device.polling_commands:
                    try:
                        # Prepare to make request
                        self.current_command = command
                        self.notify_future = self.hass.loop.create_future()
                        self.notify_response = bytearray()

                        # Make request
                        self.logger.debug("Requesting %s", command)
                        await client.write_gatt_char(
                            BluetoothClient.WRITE_UUID, bytes(command)
                        )

                        # Wait for response
                        res = await asyncio.wait_for(
                            self.notify_future, timeout=BluetoothClient.RESPONSE_TIMEOUT
                        )

                        # Process data
                        self.logger.debug("Got %s bytes", len(res))
                        response = cast(bytes, res)
                        body = command.parse_response(response)
                        parsed = self.bluetti_device.parse(
                            command.starting_address, body
                        )

                        self.logger.warning("Parsed data: %s", parsed)
                        parsed_data.update(parsed)

                    except TimeoutError:
                        self.logger.error("Polling timed out (address: %s)", self._address)
                    except ParseError:
                        self.logger.debug("Got a parse exception...")
                    except ModbusError as err:
                        self.logger.debug(
                            "Got an invalid request error for %s: %s",
                            command,
                            err,
                        )
                    except (BadConnectionError, BleakError) as err:
                        self.logger.debug("Needed to disconnect due to error: %s", err)
        except TimeoutError:
            self.logger.error("Polling timed out")
            return None
        except BleakError as err:
            self.logger.error("Bleak error: %s", err)
            return None
        finally:
            await client.disconnect()

        # Pass data back to sensors
        return parsed_data

    def _notification_handler(self, _sender: int, data: bytearray):
        """Handle bt data."""

        # Ignore notifications we don't expect
        if not self.notify_future or self.notify_future.done():
            return

        # If something went wrong, we might get weird data.
        if data == b"AT+NAME?\r" or data == b"AT+ADV?\r":
            err = BadConnectionError("Got AT+ notification")
            self.notify_future.set_exception(err)
            return

        # Save data
        self.notify_response.extend(data)

        if len(self.notify_response) == self.current_command.response_size():
            if self.current_command.is_valid_response(self.notify_response):
                self.notify_future.set_result(self.notify_response)
            else:
                self.notify_future.set_exception(ParseError("Failed checksum"))
        elif self.current_command.is_exception_response(self.notify_response):
            # We got a MODBUS command exception
            msg = f"MODBUS Exception {self.current_command}: {self.notify_response[2]}"
            self.notify_future.set_exception(ModbusError(msg))