"""N1MM contact broadcasts and plain ADIF, without third-party dependencies."""

import datetime as dt
import decimal
import re
import xml.etree.ElementTree as ET


class IgnoredPacket(ValueError):
    pass


# Frequencies in MHz. Region-specific allocations are deliberately not enforced.
BANDS = [(0.1357, 0.1378, "2190m"), (0.472, 0.479, "630m"),
         (1.8, 2, "160m"), (3.5, 4, "80m"), (5, 5.5, "60m"),
         (7, 7.3, "40m"), (10.1, 10.15, "30m"), (14, 14.35, "20m"),
         (18.068, 18.168, "17m"), (21, 21.45, "15m"), (24.89, 24.99, "12m"),
         (28, 29.7, "10m"), (50, 54, "6m"), (70, 71, "4m"),
         (144, 148, "2m"), (219, 225, "1.25m"), (420, 450, "70cm"),
         (902, 928, "33cm"), (1240, 1300, "23cm"), (2300, 2450, "13cm"),
         (3300, 3500, "9cm"), (5650, 5925, "6cm"), (10000, 10500, "3cm")]


def band_for(freq):
    for low, high, name in BANDS:
        if low <= float(freq) <= high:
            return name
    return ""


def frequency(value):
    try:
        result = decimal.Decimal(value.replace(",", ".")) / 100000
        if not result.is_finite() or result <= 0 or result > 1000000:
            raise ValueError("Frequency is outside the supported range")
        return format(result.normalize(), "f")
    except decimal.InvalidOperation as exc:
        raise ValueError("Invalid N1MM frequency (expected units of 10 Hz)") from exc


def validate(fields):
    for key in ("CALL", "QSO_DATE", "TIME_ON", "MODE"):
        if not fields.get(key):
            raise ValueError(f"Missing {key}")
    fields["CALL"] = fields["CALL"].upper()
    if not re.fullmatch(r"[A-Z0-9/]+", fields["CALL"]):
        raise ValueError("Callsign contains unsupported characters")
    date, time = fields["QSO_DATE"], fields["TIME_ON"]
    if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"\d{4}(\d{2})?", time):
        raise ValueError("Invalid QSO date or UTC time")
    fields["TIME_ON"] = time.ljust(6, "0")
    dt.datetime.strptime(date + fields["TIME_ON"], "%Y%m%d%H%M%S")
    fields["MODE"] = fields["MODE"].upper()
    if fields["MODE"] in ("USB", "LSB"):
        fields["SUBMODE"], fields["MODE"] = fields["MODE"], "SSB"
    if fields["MODE"] in ("PSK31", "PSK63", "PSK125", "PSK250"):
        fields["SUBMODE"], fields["MODE"] = fields["MODE"], "PSK"
    if fields["MODE"] == "FT4":
        fields["MODE"], fields["SUBMODE"] = "MFSK", "FT4"
    if fields["MODE"] == "FSK":
        fields["MODE"] = "RTTY"
    for key in ("BAND", "BAND_RX"):
        if fields.get(key):
            fields[key] = fields[key].lower()
    if not fields.get("FREQ") and not fields.get("BAND"):
        raise ValueError("Missing frequency or band")
    if fields.get("FREQ"):
        try:
            freq = decimal.Decimal(fields["FREQ"])
            if not freq.is_finite() or freq <= 0:
                raise ValueError("Invalid ADIF frequency")
        except decimal.InvalidOperation as exc:
            raise ValueError("Invalid ADIF frequency") from exc
        fields["FREQ"] = format(freq.normalize(), "f")
        if not fields.get("BAND"):
            fields["BAND"] = band_for(freq)
    return fields


def parse_adif(text):
    """Read a single complete record, respecting field lengths (including '<')."""
    data = text.encode("utf-8")
    fields, pos, ended = {}, 0, False
    pattern = re.compile(rb"<([A-Za-z0-9_]+)(?::(\d+)(?::[A-Za-z])?)?>")
    while pos < len(data):
        match = pattern.search(data, pos)
        if not match:
            if data[pos:].strip():
                raise ValueError("Malformed ADIF tail")
            break
        name = match[1].decode().upper()
        pos = match.end()
        if name == "EOH":
            fields.clear()
            continue
        if name == "EOR":
            if data[pos:].strip():
                raise ValueError("Send one QSO per datagram")
            ended = True
            break
        if match[2] is None:
            raise ValueError(f"Missing ADIF field length: {name}")
        size = int(match[2])
        if pos + size > len(data):
            raise ValueError("Truncated ADIF field")
        fields[name] = data[pos:pos + size].decode("utf-8").strip()
        pos += size
    if not ended:
        raise ValueError("ADIF packet has no EOR marker")
    return validate(fields)


def to_adif(fields):
    return "".join(f"<{key}:{len(value.encode('utf-8'))}>{value}"
                   for key, value in sorted(fields.items()) if value) + "<EOR>"


def unwrap_log_command(text):
    """Read SDR-Control's length-prefixed Log/parameters ADIF envelope."""
    data = text.encode("utf-8")
    command = re.match(rb"<command:(\d+)>", data, re.IGNORECASE)
    if not command:
        raise ValueError("Malformed ADIF command envelope")
    end = command.end() + int(command[1])
    if end > len(data):
        raise ValueError("Truncated ADIF command")
    if data[command.end():end].lower() != b"log":
        raise IgnoredPacket("ADIF command is not a new Log request")
    remaining = data[end:].lstrip()
    parameters = re.match(rb"<parameters:(\d+)>", remaining, re.IGNORECASE)
    if not parameters:
        raise ValueError("ADIF Log command is missing parameters")
    size = int(parameters[1])
    payload = remaining[parameters.end():]
    if len(payload) < size:
        raise ValueError("Truncated ADIF command parameters")
    if payload[size:].strip():
        raise ValueError("Unexpected data after ADIF command parameters")
    return payload[:size].decode("utf-8")


def parse_packet(data):
    if data.startswith(b"\xad\xbc\xcb\xda"):
        raise ValueError("WSJT-X binary packet: select N1MM or Log4OM in SDR-Control")
    text = data.decode("utf-8-sig").strip("\x00 \r\n\t")
    if re.match(r"<command:", text, re.IGNORECASE):
        return parse_adif(unwrap_log_command(text)), "SDR-Control ADIF", ""
    if re.match(r"<(?:\w+:\d+|ADIF_VER:|EOH>)", text, re.IGNORECASE) or (
        "<EOH>" in text.upper() and not text.startswith("<?xml")):
        return parse_adif(text), "ADIF", ""
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML declarations with entities are not accepted")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid N1MM XML: {exc}") from exc
    kind = root.tag.split("}")[-1].lower()
    if kind in ("contactreplace", "contactdelete"):
        raise IgnoredPacket(f"{kind}: edit or delete the existing QSO in CloudLog manually")
    if kind != "contactinfo":
        raise IgnoredPacket(f"{kind}: not a newly logged QSO")
    values = {el.tag.split("}")[-1].lower(): (el.text or "").strip() for el in root}
    fields = {}
    mapping = {"call": "CALL", "mycall": "STATION_CALLSIGN", "operator": "OPERATOR",
               "mode": "MODE", "snt": "RST_SENT", "rcv": "RST_RCVD",
               "gridsquare": "GRIDSQUARE", "mygridsquare": "MY_GRIDSQUARE",
               "name": "NAME", "qth": "QTH", "comment": "COMMENT",
               "sntnr": "STX", "rcvnr": "SRX", "power": "RX_PWR",
               "satname": "SAT_NAME", "satmode": "SAT_MODE", "propmode": "PROP_MODE"}
    for source, target in mapping.items():
        if values.get(source):
            fields[target] = values[source]
    stamp = values.get("timestamp", "")
    try:
        date = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if date.tzinfo:
            date = date.astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise ValueError("Missing or invalid N1MM UTC timestamp") from exc
    fields.update(QSO_DATE=date.strftime("%Y%m%d"), TIME_ON=date.strftime("%H%M%S"))
    tx, rx = values.get("txfreq", ""), values.get("rxfreq", "")
    if tx and tx != "0":
        fields["FREQ"] = frequency(tx)
    elif rx and rx != "0":
        fields["FREQ"] = frequency(rx)
    if rx and rx != "0" and fields.get("FREQ") != frequency(rx):
        fields["FREQ_RX"] = frequency(rx)
        fields["BAND_RX"] = band_for(fields["FREQ_RX"])
    if not fields.get("FREQ"):
        band = values.get("band", "").lower().replace(",", ".")
        if band in {name for _, _, name in BANDS}:
            fields["BAND"] = band
        elif band:
            try:
                fields["BAND"] = band_for(float(band))
            except ValueError:
                pass
    return validate(fields), "N1MM", values.get("id", "")
