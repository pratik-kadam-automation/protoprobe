"""All the help text of ProtoProbe in ONE place: the first-run tour, the Help window (F1), the field tooltips and docs/USER-GUIDE.md are all
built from this module, so they cannot disagree. Plain data and pure functions, no screens.

Rules for writing here: describe only what the app really does (a test checks that every sidebar tab has an entry); use synthetic example
values only (this repository is public: no real client, site, machine or IP); say in words what the user can skip.
"""

SAFETY = ("ProtoProbe only READS. Nothing in it writes a value to a PLC, and the Capture traffic window only listens: it sends nothing. "
          "It never changes a setting on the PLC or on this PC; where a setting must change, it tells you what to ask for and who decides.")

# the sidebar, in order: (group title, [screen keys])
GROUPS = [
    ("Overview", ["dashboard"]),
    ("Connect", ["connections", "live_values"]),
    ("Discover", ["scan_results", "network", "modbus_test"]),
    ("Commission", ["commission", "evidence", "oee", "output"]),
    ("System", ["settings"]),
]

SCREENS = {
    "dashboard": {
        "title": "Dashboard",
        "summary": "A one-page overview: your connections with their status, and the OEE estimate from the formula you set under OEE config.",
        "use_when": "Open it first to see what is connected and what to do next. The Start here list ticks itself off as you go.",
        "needs": [],
        "steps": ["Read the Start here list: it shows the next step.", "Look at the connection list for each connection's status."],
        "watch": ["The OEE cards stay at -- until you configure OEE and start polling."],
        "skip": "Nothing to configure here. Skip it once you know your way around.",
    },
    "connections": {
        "title": "Connections",
        "summary": "Where you tell ProtoProbe how to reach each PLC (protocol, PLC type, address) and where you read from it.",
        "use_when": "Always the first real step: nothing else works until there is a connection.",
        "needs": ["The PLC's IP address (or serial settings), and for some PLCs a port, rack and slot. Hover a field to see what it means."],
        "steps": [
            "Add Connection: choose the PLC type (it fills in sensible defaults), type the IP, Save.",
            "Read a Value: read one address to prove the link works before anything else.",
            "Scan for Live Tags: read a range of addresses a few times and see which ones change (finds candidate tags).",
            "Manage Tags / Import tags from file: build the list of tags to poll.",
            "Start All: poll the tags on every enabled connection. Stop All stops it.",
            "If a read fails: Connection doctor (Mitsubishi) or Capture traffic (any connection with an IP) say why.",
        ],
        "watch": ["A Mitsubishi Q series has NO default MC port: it is the one set under Open settings in the PLC. Choose tcp or udp to match it.",
                  "Save Multi-PLC Config / Load Multi-PLC Config write and read a configuration file; your connections are also remembered between sessions."],
        "skip": "Not skippable: this is where everything starts.",
    },
    "live_values": {
        "title": "Live values",
        "summary": "Every tag of every connection with its current value, quality (Good or Bad), time of the last read and the reason when a read fails.",
        "use_when": "After Start All, to watch the values and to choose which tags go into the output JSON.",
        "needs": ["Tags on at least one connection, and polling started (Connections > Start All)."],
        "steps": ["Start All on Connections.", "Watch Value and Quality. A Bad quality shows the PLC's reason in the Error column.",
                  "Select rows and press Include selected in output JSON to send them under a name you choose.",
                  "Double-click a row to correct a tag while it is being read."],
        "watch": ["Nothing is written to the PLC when you edit a tag: only ProtoProbe's own tag list changes."],
        "skip": "Skip it if you only want to scan or commission.",
    },
    "scan_results": {
        "title": "Scan results",
        "summary": "What your scans found, across all connections: addresses whose value changed first, with a guess at what each could be.",
        "use_when": "After Connections > Scan for Live Tags, to pick the addresses worth turning into tags.",
        "needs": ["A finished (or running) scan."],
        "steps": ["Run a scan from Connections.", "Look at the Changed? column: an address that changed while the machine ran is a candidate.",
                  "Select rows and Add selected to tag list, or Add + include in output JSON.",
                  "Export suggestions writes the candidates to CSV or JSON.",
                  "Search for a known value: type a number you can see on the machine's screen to find where the PLC keeps it."],
        "watch": ["The suggested use is a GUESS from how a value moved in a few snapshots. Confirm it on the machine before you rely on it.",
                  "A scan that read nothing says so under its title and shows no rows: check Read a Value first."],
        "skip": "Skip it if you already know your addresses (for example from a tag export).",
    },
    "network": {
        "title": "Network tools",
        "summary": "Find devices on this PC's network and check which industrial ports are open: an IP scanner (with the ARP table) and a port scanner.",
        "use_when": "When you do not know a PLC's address or port.",
        "needs": ["This PC on the same network segment as the devices."],
        "steps": ["IP scanner: enter the first three numbers, a start and an end, Scan.", "Port scanner: enter the PLC's IP and a port range (for example 5000-5020), Scan."],
        "watch": ["A device that ignores ping can still show as ARP only. Only this PC's own network segment is covered."],
        "skip": "Skip it when you already know the IP and port.",
    },
    "modbus_test": {
        "title": "Modbus test",
        "summary": "A ModScan-style Modbus tester (TCP over Ethernet or RTU over a serial line): see registers in several number formats, the raw bytes and the device's answer.",
        "use_when": "To check a Modbus device by hand, find its unit ID, or see exactly what it answers.",
        "needs": ["For TCP: IP and port. For RTU: a COM port with the right baud rate, parity and stop bits."],
        "steps": ["Choose Modbus TCP or RTU.", "Set unit, function (1 to 4) and the start address and count.", "Read now, or poll; use Find unit IDs if the unit is unknown."],
        "watch": ["Only the read functions 1 to 4 exist: there is no write function.",
                  "A value that looks absurd (for example 7e-40) usually means the wrong word order: try the other orders."],
        "skip": "Skip it if you do not use Modbus.",
    },
    "commission": {
        "title": "Commissioning",
        "summary": "A guided wizard that finds each signal's address and proves it against the machine's HMI, then exports a commissioning pack for the dashboard.",
        "use_when": "When you set up a new machine and want every tag checked before the gateway goes live.",
        "needs": ["A PLC connection (or practice mode), and someone at the machine who can read the HMI."],
        "steps": ["1 Questions: describe the machine.", "2 Readings: record readings at moments you choose, while someone confirms the HMI values.",
                  "3 Review: check what was found.", "4 Export: write the commissioning pack the dashboard imports."],
        "watch": ["Practice mode uses a simulated PLC: it needs no hardware and is the safe way to learn the wizard."],
        "skip": "Skip it if you only need to read values or diagnose a link.",
    },
    "evidence": {
        "title": "Evidence",
        "summary": "Builds the file that the dashboard's AI review reads, from the Commissioning answers and the candidate tags of your scan, with names, IPs and similar details removed.",
        "use_when": "Only when you want the dashboard's AI review of a machine.",
        "needs": ["Commissioning answers and scan results."],
        "steps": ["Check the list of answers that are still missing.", "Add any extra words to remove (client, site, machine, people).",
                  "Read the exact text shown, tick the box, then Save or Copy.", "Upload the file on the dashboard (Commissioning page > AI review)."],
        "watch": ["Nothing leaves this PC from here. The table that maps T001... back to real addresses stays on screen only."],
        "skip": "Optional: skip it if you do not use the AI review.",
    },
    "oee": {
        "title": "OEE config",
        "summary": "Defines how availability, performance and quality are calculated from your tags, with test values to try the formula.",
        "use_when": "When you want an OEE estimate on the Dashboard.",
        "needs": ["Tag names (from Live values) to use in the formula."],
        "steps": ["Optionally fill the standard mapping.", "Type or adjust the formula using your tag names.", "Enter test values to see the result before using live data."],
        "watch": ["The result is an estimate from the tags you map: it is only as good as that mapping."],
        "skip": "Optional: skip it if you do not need OEE.",
    },
    "output": {
        "title": "Output",
        "summary": "The JSON message built from your live values, and the settings for publishing it over MQTT (client ID, QoS, TLS and so on).",
        "use_when": "When the values must leave this PC as JSON, for example to test a broker.",
        "needs": ["Tags included in the output JSON; for MQTT, a broker address and credentials."],
        "steps": ["Edit the template or use the commissioning layout.", "Set the MQTT broker and options if you publish.", "Start All on Connections: the log shows each message built."],
        "watch": ["Use a separate MQTT client ID for tests. A test that reuses a production client ID can disconnect production.",
                  "Turn the certificate check off only for a test broker."],
        "skip": "Optional: skip it if you do not publish data from ProtoProbe.",
    },
    "settings": {
        "title": "Settings",
        "summary": "Appearance (dark or light) and information about this version.",
        "use_when": "To change the look, or to find the version number when you report a problem.",
        "needs": [],
        "steps": ["Choose Dark or Light."],
        "watch": [],
        "skip": "Skip it unless you want to change the theme.",
    },
}

TROUBLESHOOTING = [
    ("No answer from the PLC (timeout)",
     ["Wrong IP or port.", "This PC is in another network (check the subnet mask: 0.0.0.0 is not valid).", "A firewall drops the traffic.",
      "The PLC's Ethernet port is not set up, or the settings were not written and the PLC not power-cycled.", "The cable or switch."],
     ["Ping the PLC. Run Connection doctor (Mitsubishi). Use Capture traffic (run as administrator) to see whether the PLC answers at all."]),
    ("Connection refused",
     ["The PLC was reached but nothing listens on that port (over TCP).", "The PLC may use UDP on that port instead."],
     ["Check the port in the PLC's settings. For Mitsubishi MC, try transport udp."]),
    ("It works in another program but not in ProtoProbe",
     ["The other program may use UDP and ProtoProbe TCP (or the other way round).", "A firewall rule may allow only the other program.", "A different port, IP or network adapter."],
     ["Compare IP, port and tcp/udp in both. Run Connection doctor: it tries UDP and TCP on the port and says which answers."]),
    ("Mitsubishi Q series: which port?",
     ["There is no default MC port: it is the one set under Open settings in GX Works.", "A name like Q64PN is the power supply, not the CPU.",
      "Only CPUs with E or V in the name have built-in Ethernet."],
     ["Scan ports 5000-5020 under Network tools. After writing parameters to the PLC, power-cycle it once."]),
    ("Siemens: connects but data blocks cannot be read",
     ["PUT/GET access is not allowed in the PLC.", "The block uses optimized access.", "The block is empty (no variables).", "Wrong rack or slot."],
     ["Check PUT/GET and optimized access in TIA Portal; S7-1200/1500 use rack 0 slot 1."]),
    ("A scan says Done but shows no rows",
     ["Nothing could be read: every address failed. The scan now says 'Done -- nothing could be read' and shows what it reported."],
     ["Use Read a Value on one address first. When that works, scan again. Run Connection doctor or Capture traffic to see why not."]),
    ("A value looks absurd (for example 7e-40)",
     ["The 32-bit word order is wrong, or the data type or address is wrong."],
     ["Try another word order or the Int16 type; confirm the value on the machine's screen."]),
    ("Capture traffic shows nothing or says it needs administrator",
     ["The capture needs the program to run as administrator on Windows.", "Some Windows versions do not show outgoing packets."],
     ["Right-click ProtoProbe > Run as administrator. Start the capture, then do the read, then Stop. Use Connection doctor if you cannot run as administrator."]),
    ("Windows SmartScreen warns when starting",
     ["The program is not code-signed."],
     ["Check the file's SHA-256 against the release's SHA256SUMS.txt, then choose More info > Run anyway if your IT allows it."]),
    ("A window or a button is cut off",
     ["The window is too small for its content."],
     ["Enlarge or maximize the window, and report it with Help > Export diagnostics."]),
]

FIELD_HELP = {
    "ip": "The PLC's IP address, for example 192.168.1.50. This PC must be in the same network (the first three numbers equal with mask 255.255.255.0) or have a route to it.",
    "port": "The TCP/UDP port. Modbus TCP is usually 502. Mitsubishi MC has NO default: it is the port set under Open settings in the PLC. Not known? Network tools > Port scanner, or Connection doctor.",
    "transport": "tcp or udp. For Mitsubishi MC it must match the protocol chosen under Open settings in the PLC. If a read times out on tcp, try udp.",
    "rack": "Siemens rack number. S7-1200 and S7-1500 use 0.",
    "slot": "Siemens CPU slot. S7-1200 and S7-1500 use 1, S7-300 usually 2. For an S7-400 it depends on the hardware layout.",
    "baudrate": "Serial speed. It must match the device exactly (common: 9600, 19200).",
    "parity": "Serial parity: N (none), E (even) or O (odd). It must match the device.",
    "stopbits": "Serial stop bits: 1 or 2. It must match the device.",
    "bytesize": "Serial data bits, normally 8.",
    "endpoint": "The OPC UA server address, for example opc.tcp://192.168.1.70:4840.",
    "local_ip": "This PC's own IP address on the network that reaches the device (BACnet).",
    "device_instance": "ProtoProbe's own made-up BACnet device number. Any unused number works.",
    "device_ip": "The BACnet device's IP address.",
    "remote_device_instance": "The TARGET device's own instance number. Read it from the device's configuration; do not invent it.",
    "device_port": "The BACnet UDP port, normally 47808.",
}

SHORTCUTS = [
    ("F1", "Help for the screen you are on"),
    ("Ctrl+H", "Open the Help window"),
    ("Esc", "Close a help or tour window"),
]


def screen_help(key):
    return SCREENS[key]


def group_of(key):
    for title, keys in GROUPS:
        if key in keys:
            return title
    return ""


def tour_cards():
    """The first-run tour: a welcome card, one card per screen in sidebar order, and a closing card. [(title, body)]."""
    cards = [("Welcome to ProtoProbe",
              "ProtoProbe helps you connect to a PLC, check that its values can be read, find the tags that matter and commission a machine.\n\n"
              + SAFETY + "\n\nThis short tour explains each screen. You can skip it now and open it again any time from Help > Take the tour.")]
    for _, keys in GROUPS:
        for key in keys:
            s = SCREENS[key]
            cards.append((s["title"], f"{s['summary']}\n\nUse it: {s['use_when']}\n\nCan I skip it? {s['skip']}"))
    cards.append(("You are ready",
                  "Start with Connections: add a connection, then read one value. If something does not work, open Help > Troubleshooting, or run "
                  "Connection doctor / Capture traffic on the Connections screen. Press F1 on any screen for help about that screen."))
    return cards


def user_guide_markdown():
    """docs/USER-GUIDE.md, generated (tools/gen_user_guide.py). A test fails when the file differs from this."""
    out = ["# ProtoProbe user guide", "", "<!-- generated by tools/gen_user_guide.py from help_content.py: edit help_content.py, not this file -->", "",
           SAFETY, "", "## Where to start", "",
           "1. Connections: add a connection, then Read a Value.", "2. If a read fails: Connection doctor or Capture traffic, and the Troubleshooting section below.",
           "3. Scan for Live Tags, then Scan results, to find tags. Commissioning when you set up a machine.", ""]
    for title, keys in GROUPS:
        out += [f"## {title}", ""]
        for key in keys:
            s = SCREENS[key]
            out += [f"### {s['title']}", "", s["summary"], "", f"**Use it:** {s['use_when']}", ""]
            if s["needs"]:
                out += ["**You need:**"] + [f"- {n}" for n in s["needs"]] + [""]
            out += ["**Steps:**"] + [f"{i}. {t}" for i, t in enumerate(s["steps"], 1)] + [""]
            if s["watch"]:
                out += ["**Watch out:**"] + [f"- {w}" for w in s["watch"]] + [""]
            out += [f"**Can I skip it?** {s['skip']}", ""]
    out += ["## Troubleshooting", ""]
    for symptom, causes, todo in TROUBLESHOOTING:
        out += [f"### {symptom}", "", "Usual causes:"] + [f"- {c}" for c in causes] + ["", "What to do:"] + [f"- {t}" for t in todo] + [""]
    out += ["## Field help", ""] + [f"- **{k}**: {v}" for k, v in FIELD_HELP.items()] + [""]
    out += ["## Keyboard shortcuts", ""] + [f"- **{k}**: {v}" for k, v in SHORTCUTS] + [""]
    return "\n".join(out)
