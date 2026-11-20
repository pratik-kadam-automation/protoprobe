"""s7_address.py -- the guided way to name one Siemens value: a register type (I, Q, M, DB) and a width (bit, byte, word, dword), then the address.

Pure helpers, no screens, so they can be tested anywhere. The tags they build use the same fields as the rest of ProtoProbe
(area, db_number, start_byte, bit_offset, data_type, multiplier, offset) and are read by edge-gateway/drivers/s7_driver.py.
Byte values (IB, MB, DBB) are listed because they exist on the PLC but ProtoProbe's S7 driver cannot read them yet: `readable(width)` says so.
"""

AREAS = ("I", "Q", "M", "DB")
WIDTHS = ("bit", "byte", "word", "dword")

_DATA_TYPES = {                   # width -> data types the driver decodes; the first is the default
    "bit": ("bit",),
    "word": ("int16", "uint16"),
    "dword": ("int32", "uint32", "float32_be"),
}
_FRIENDLY = {"bit": "Bool", "int16": "Int16", "uint16": "UInt16", "int32": "Int32 (DInt)", "uint32": "UInt32 (UDInt)", "float32_be": "Float (Real)"}


def readable(width):
    """Can ProtoProbe's S7 driver read this width today?"""
    return width in _DATA_TYPES


def data_types_for(width):
    return list(_DATA_TYPES.get(width, ()))


def friendly_type(data_type):
    return _FRIENDLY.get(data_type, data_type)


def address_text(area, width, db, byte, bit=0):
    """'DB1.DBW0', 'I0.3', 'MD60', 'QB2' ..., in the notation TIA Portal uses (without the %)."""
    if area not in AREAS or width not in WIDTHS:
        raise ValueError(f"unknown area/width: {area!r}/{width!r}")
    byte, bit = int(byte), int(bit)
    if byte < 0:
        raise ValueError("the byte offset cannot be negative")
    if width == "bit":
        if not 0 <= bit <= 7:
            raise ValueError("the bit must be 0 to 7")
        return f"DB{int(db)}.DBX{byte}.{bit}" if area == "DB" else f"{area}{byte}.{bit}"
    letter = {"byte": "B", "word": "W", "dword": "D"}[width]
    return f"DB{int(db)}.DB{letter}{byte}" if area == "DB" else f"{area}{letter}{byte}"


def build_tag(name, area, width, db, byte, bit=0, data_type=None, scale=1.0, offset=0.0, unit=""):
    """The tag dict for the connection. Raises ValueError with a plain message for anything that cannot be read."""
    name = (name or "").strip()
    if not name:
        raise ValueError("The tag needs a name.")
    if not readable(width):
        raise ValueError("ProtoProbe cannot read single bytes yet: use Word or DWord (or Bit).")
    address_text(area, width, db, byte, bit)                       # validates area, width, byte, bit
    options = data_types_for(width)
    data_type = data_type or options[0]
    if data_type not in options:
        raise ValueError(f"Data type {data_type} does not fit a {width}: choose one of {', '.join(options)}.")
    if area == "DB" and int(db) < 1:
        raise ValueError("The DB number must be 1 or more.")
    tag = {"name": name, "area": area, "db_number": int(db) if area == "DB" else 0, "start_byte": int(byte),
           "bit_offset": int(bit) if width == "bit" else 0, "data_type": data_type,
           "multiplier": float(scale), "offset": float(offset)}
    if unit:
        tag["unit"] = unit
    return tag
