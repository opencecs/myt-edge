#!/usr/bin/env python3
"""Cross-check the Y12 slot mapping documented from kernel and MCU sources."""

import argparse
import re
from pathlib import Path


def verify_slot_gpios(dts, channels):
    hotplug = dts.split("myt_slot_hotplug: myt-slot-hotplug {", 1)[1].split("};", 1)[0]
    slots = list(map(int, re.search(r"myt,physical-slots = <([^>]+)>", hotplug)[1].split()))
    entries = re.search(r"det-gpios = ([^;]+);", hotplug)[1]
    gpios = re.findall(r"<&(tca9555_[12]) (\d+) GPIO_ACTIVE_LOW>", entries)
    if len(gpios) != 8 or len(slots) != 8 or len(set(slots)) != 8 or not set(slots) <= set(range(1, 13)):
        raise ValueError("expected eight distinct slots with active-low presence inputs")
    expected_offsets = {(chip, str(offset)) for chip in ("tca9555_1", "tca9555_2")
                        for offset in range(12, 16)}
    if set(gpios) != expected_offsets:
        raise ValueError("expected presence inputs on both expanders at offsets 12–15")
    for slot, (chip, offset) in zip(slots, gpios):
        node = re.search(rf"{chip}: tca9555@\w+ {{([^}}]+)}};", dts)[1]
        names = re.search(r"gpio-line-names = ([^;]+);", node)[1]
        line_names = re.findall(r'"([^"]*)"', names)
        if len(line_names) != 16 or line_names[int(offset)] != f"y12-slot{slot}-present":
            raise ValueError(f"presence GPIO differs from its physical slot: {slot}")

    entries = re.search(r"myt,slot-power-gpios = ([^;]+);", dts)[1]
    expected = [(0, 3), (0, 5), (0, 9), (0, 11),
                (0, 15), (1, 1), (1, 5), (1, 7)]
    gpios = [(int(chip), int(offset)) for chip, offset in
             re.findall(r"<&tca9555_([01]) (\d+) GPIO_ACTIVE_HIGH>", entries)]
    expected_letters = "BCEFHIKL"
    if (gpios != expected or entries.count("<") != len(expected) or
            "".join(chr(ord("A") + channels[slot - 1]) for slot in slots) != expected_letters):
        raise ValueError("x1 slot power GPIOs differ from the Y12 schematic")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dts", type=Path, required=True)
    parser.add_argument("--mcu-header", type=Path, required=True)
    args = parser.parse_args()

    dts = args.dts.read_text()
    header = args.mcu_header.read_text()
    document = (Path(__file__).resolve().parents[1] / "docs/Y4-Y12-DIFFERENCES.md").read_text()

    ports = list(map(int, re.search(r"myt,slot-downstream-ports = <([^>]+)>", dts)[1].split()))
    channels = list(map(int, re.search(r"uint8_t channel\[\] = \{([^}]+)", header)[1].split(",")))
    led_rows = re.findall(r'label = "slot(\d+):run";\s*gpios = <&tca9555_3 (\d+)', dts)
    leds = {int(slot): int(pin) for slot, pin in led_rows}

    if len(ports) != 12 or sorted(ports) != list(range(12)):
        raise ValueError("expected each IX8024 port device number once")
    if len(channels) != 12 or sorted(channels) != list(range(12)):
        raise ValueError("expected each of the twelve INA3221 channels once")
    if len(led_rows) != 12 or set(leds) != set(range(1, 13)) or len(set(leds.values())) != 12:
        raise ValueError("expected twelve independent slot running lights")

    for slot, (port, channel) in enumerate(zip(ports, channels), 1):
        row = (f"| {slot} | {port} | 0x{0x40 + channel // 3:02X} / {channel % 3 + 1} | "
               f"{channel + 1} | {leds[slot]} |")
        if row not in document.splitlines():
            raise ValueError(f"documented mapping differs for slot {slot}: {row}")
    verify_slot_gpios(dts, channels)
    print("Y12: 12 slot mappings and 8 presence and boot power GPIO pairs verified")


if __name__ == "__main__":
    main()
