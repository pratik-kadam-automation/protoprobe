# What the gateway offers per protocol (reference for ProtoProbe)

The PUSR M300 gateway (WukongEdge firmware V1.1.90) reads a PLC through "data points". Its *Add node* dialog shows, per protocol, which register types it knows.
This file records those lists as the owner saw them on 2026-11-19, so that ProtoProbe can offer the same protocols and the same names, and what ProtoProbe proves
will also work on the gateway. Source: screenshots of the dialog, nothing else. **Nothing here is a verified address map: take real addresses from the PLC manual.**
No client, site, machine or network names belong in this file (the repository is public).

## The dialog (all protocols)
Node name (required), Node desc, **Register** (a register flag, a number, and a green preview such as `00001(bit)` or `X0(bit)`), a data-type list, Read/Write,
Acquisition formula, Control formula, **Timeout** (default 2000 ms), Unit.

## Protocol list in the gateway
Virtual Slave, Modbus_TCP, Modbus_RTU, Modbus_ASC, DVP_RTU, FATEK_COM, FX1S_COM, FX2N(1N)_COM, FX3U_COM, FX5U_COM, FX5U_TCP, FX3U_ADP_TCP, Q_TCP, S7_200 Smart,
S7_1200, S7_200 PPI, S7_1500, FINS_TCP, FINS_COM, S7-300, S7-400, DLT-645-2007, IEC_104, BACnet IP, IEC61850, h3_5u_tcp, DNP3, EtherNet/IP (Unconnected),
EtherNet/IP (Compactlogix).

## In code
`plc_catalog.py` holds these lists as data (`CATALOG`: all 29 gateway PLC types; `connectable()`: the 8 ProtoProbe can connect to today; `readable_areas()`; `address_preview()`).
`tests/test_plc_catalog.py` checks it, including that nothing is called readable unless the driver really reads it. No screen uses it yet.

## Seen so far

### Modbus_TCP, Modbus_RTU, Modbus_ASC (one list for all three)
| Flag | Area |
|---|---|
| 0 | Coils status |
| 1 | Discrete inputs |
| 3 | Input registers |
| 4 | Holding registers |

The preview `00001(bit)` for flag 0, number 1 shows the usual five-digit Modbus notation counted **from 1**. ProtoProbe's Modbus addresses are a different convention
until proven otherwise: convert deliberately, and test against a simulator.

### DVP_RTU (Delta)
Register flags: `X` input relay, `Y` output relay, `M` auxiliary relay, `S` step relay, `T` timer relay, `TV` timer value, `C` counter relay, `CV` counter value,
`D` data register. (The dialog labels `CV` and `D` as "Timer Value": almost certainly a typing mistake in the gateway.) Preview style `X0(bit)`.
The mapping of these names to Modbus addresses must come from the Delta manual.

### FATEK_COM
Each register flag has a **Bit / Word (16-bit) / DWord (32-bit)** form; the flag itself decides the width:
| Area | Bit | Word | DWord |
|---|---|---|---|
| X input relay | X | WX | DWX |
| Y output relay | Y | WY | DWY |
| M internal relay | M | WM | DWM |
| S step relay | S | WS | DWS |
| T timer relay | T | WT | DWT |
| C counter relay | C | WC | DWC |
| TMR timer present value | - | RT | DRT |
| CTR counter present value | - | RC | DRC |
| HR data register | - | R | DR |
| DR data register | - | D | DD |

The list scrolls; entries below `DR`, if any, were not seen.

### FX1S_COM and FX2N(1N)_COM (Mitsubishi FX, serial)
One column of flags, the same in both: `X` input relay, `Y` output relay, `M0` auxiliary relay, `M8000` special auxiliary relay, `S` status relay, `T` timer flag,
`C` counter flag, `CV0` counter value (C0-C199), `CV200` counter value (C200 and up), `D0` data register, `D8000` special data register (the FX2N list was seen only down to `T`).
The flag chooses where an area starts (`M0` against `M8000`), and the number is the offset inside it. No timer-value flag was seen.
On Mitsubishi FX the X and Y numbers are written in **octal** (X0-X7, then X10): check the manual before turning them into decimal addresses.

## Still to collect (priority order)
FX5U_TCP and Q_TCP, S7_1200 / S7_1500 / S7-300 / S7-400, FINS_TCP, EtherNet/IP (Compactlogix), the Modbus_TCP device page (settings, not addresses), then S7_200 Smart,
FX3U_ADP_TCP, FX3U_COM, FX5U_COM, FINS_COM, DLT-645-2007, h3_5u_tcp. For each: the full register dropdown, the data-type list, any byte/word-order setting, the device
settings page (IP and port, or the serial settings and station number). Low priority: IEC_104, DNP3, IEC61850, BACnet IP, Virtual Slave.
