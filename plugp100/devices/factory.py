import dataclasses
import logging
from typing import Optional, Type

import aiohttp

from plugp100.api.requests.tapo_request import TapoRequest
from plugp100.api.tapo_client import TapoClient
from plugp100.common.credentials import AuthCredential
from plugp100.devices.base import TapoDevice
from plugp100.devices.bulb import TapoBulb
from plugp100.devices.hub import TapoHub
from plugp100.devices.plug import TapoPlug
from plugp100.models.device import DeviceInfo
from plugp100.errors.invalid_authentication import InvalidAuthentication
from plugp100.api.protocol.klap import klap_handshake_v1, klap_handshake_v2
from plugp100.api.protocol.klap.klap_protocol import KlapProtocol
from plugp100.api.protocol.passthrough_protocol import PassthroughProtocol
from plugp100.api.protocol.tapo_protocol import TapoProtocol

_LOGGER = logging.getLogger("DeviceFactory")


@dataclasses.dataclass
class DeviceConnectConfiguration:
    host: str
    port: int = 80
    scheme: str = "http"
    verify_ssl: bool = True
    credentials: Optional[AuthCredential] = None
    device_type: Optional[str] = None
    device_model: Optional[str] = None
    encryption_type: Optional[str] = None
    encryption_version: Optional[int] = None

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/app"


async def connect(
    config: DeviceConnectConfiguration, session: Optional[aiohttp.ClientSession] = None
):
    if config.device_type is None:
        protocol = await _get_or_guess_protocol(config, session)
        _LOGGER.debug(
            "Not enough information to detected device type and model, trying to fetching from device..."
        )
        device_info = DeviceInfo(
            **(await protocol.send_request(request=TapoRequest.get_device_info()))
            .get_or_raise()
            .result
        )
        factory = _get_device_class_from_model_type(device_info.type)
    else:
        factory = _get_device_class_from_model_type(config.device_type)
        protocol = await _get_or_guess_protocol(config, session)

    client = TapoClient(config.credentials, config.url, protocol, session)
    return factory(config.host, config.port, client)


async def _get_or_guess_protocol(
    config: DeviceConnectConfiguration, session: Optional[aiohttp.ClientSession] = None
) -> TapoProtocol:
    if config.encryption_type is None:
        return await _guess_protocol(config, session)
    if config.encryption_type.lower() == "klap":
        handshake_version = (
            klap_handshake_v2() if config.encryption_version == 2 else klap_handshake_v1()
        )
        return KlapProtocol(
            auth_credential=config.credentials,
            url=config.url,
            klap_strategy=handshake_version,
            http_session=session,
            verify_ssl=config.verify_ssl,
        )
    if config.encryption_type.lower() == "aes":
        return PassthroughProtocol(
            auth_credential=config.credentials,
            url=config.url,
            http_session=session,
            verify_ssl=config.verify_ssl,
        )
    raise Exception("Failed to determine the right tapo protocol")


async def _guess_protocol(
    config: DeviceConnectConfiguration, session: Optional[aiohttp.ClientSession] = None
) -> TapoProtocol:
    protocol = await _try_protocols_at(config, session)
    if protocol is not None:
        return protocol

    # H200 hubs (and other recent Tapo firmwares) only listen on HTTPS:443
    # with a self-signed TPRI-DEVICE certificate. If the default HTTP:80
    # transport failed completely, retry over HTTPS:443 with TLS verification
    # disabled before declaring it an authentication failure.
    if config.scheme == "http" and config.port == 80:
        _LOGGER.debug(
            "HTTP:80 protocols failed for %s, retrying over HTTPS:443", config.host
        )
        https_config = dataclasses.replace(
            config, scheme="https", port=443, verify_ssl=False
        )
        protocol = await _try_protocols_at(https_config, session)
        if protocol is not None:
            return protocol

    _LOGGER.error("None of available protocol is working, maybe invalid credentials")
    raise InvalidAuthentication(config.host, config.device_type)


async def _try_protocols_at(
    config: DeviceConnectConfiguration, session: Optional[aiohttp.ClientSession] = None
) -> Optional[TapoProtocol]:
    protocols = [
        PassthroughProtocol(
            config.credentials, config.url, session, verify_ssl=config.verify_ssl
        ),
        KlapProtocol(
            config.credentials,
            config.url,
            klap_handshake_v1(),
            session,
            verify_ssl=config.verify_ssl,
        ),
        KlapProtocol(
            config.credentials,
            config.url,
            klap_handshake_v2(),
            session,
            verify_ssl=config.verify_ssl,
        ),
    ]
    device_info_request = TapoRequest.get_device_info()
    for i, protocol in enumerate(protocols):
        try:
            info = await protocol.send_request(device_info_request)
            succeeded = info.is_success()
        except Exception as exc:
            # PassthroughProtocol.send_request lets aiohttp errors (timeouts,
            # SSL failures, refused connections, etc.) propagate as raw
            # exceptions instead of wrapping them in Try.Failure like
            # KlapProtocol does. Without this catch the first protocol failure
            # aborts the whole guess loop and the HTTPS:443 fallback below
            # never gets a chance to run.
            _LOGGER.debug(
                "Protocol %s at %s raised %s, trying next...",
                type(protocol).__name__,
                config.url,
                exc,
            )
            await protocol.close()
            continue
        if succeeded:
            _LOGGER.debug(
                "Found working protocol %s at %s", type(protocol), config.url
            )
            for j, other_protocol in enumerate(protocols):
                if i != j:
                    await other_protocol.close()
            return protocol
        _LOGGER.debug(
            "Protocol %s at %s not working, trying next...",
            type(protocol),
            config.url,
        )
        await protocol.close()
    return None


def _get_device_class_from_model_type(device_type: str) -> Type[TapoDevice]:
    device_type = device_type.upper()
    if device_type == "SMART.TAPOPLUG":
        return TapoPlug
    if device_type == "SMART.TAPOBULB":
        return TapoBulb
    if device_type == "SMART.TAPOHUB":
        return TapoHub
    if device_type == "SMART.KASAHUB":
        return TapoHub
    if device_type == "SMART.IPCAMERA":
        raise Exception(f"Device of type {device_type} not supported!")
    return TapoDevice
