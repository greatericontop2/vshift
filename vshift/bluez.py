"""Direct BlueZ (D-Bus) helpers for connections that outlive this process.

bluetoothd owns BLE connections, not the app. If the app exits without
disconnecting (crash, kill, closed window), the link stays up, and a connected
device stops advertising, so the next run can't find it. These helpers find and
drop such leftover links.
"""
from __future__ import annotations

from dataclasses import dataclass

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from . import protocol as P

BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"


@dataclass
class ConnectedDevice:
    path: str
    address: str
    name: str
    kind: str  # "trainer", "zwift:<type>" or ""


def _kind(props: dict) -> str:
    uuids = [u.lower() for u in props.get("UUIDs", _V([])).value]
    mfr = {k: bytes(v.value) for k, v in props.get("ManufacturerData", _V({})).value.items()}
    zt = P.zwift_device_type(mfr)
    if zt and zt != "hub":
        return f"zwift:{zt}"
    if P.FTMS_SERVICE in uuids or zt == "hub":
        return "trainer"
    if P.ZWIFT_SERVICE in uuids or P.ZWIFT_SERVICE_FC82 in uuids:
        return "zwift"
    return ""


class _V:  # stand-in for a missing dbus Variant
    def __init__(self, value):
        self.value = value


async def connected_devices() -> list[ConnectedDevice]:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect(BLUEZ, "/")
        om = bus.get_proxy_object(BLUEZ, "/", intro).get_interface("org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
    finally:
        bus.disconnect()
    out = []
    for path, ifaces in objects.items():
        props = ifaces.get(DEVICE_IFACE)
        if props and props.get("Connected", _V(False)).value:
            out.append(ConnectedDevice(path, props["Address"].value,
                                       props.get("Name", _V("")).value, _kind(props)))
    return out


async def disconnect(path: str) -> None:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        intro = await bus.introspect(BLUEZ, path)
        await bus.get_proxy_object(BLUEZ, path, intro).get_interface(DEVICE_IFACE).call_disconnect()
    finally:
        bus.disconnect()
