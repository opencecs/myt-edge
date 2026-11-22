# Y4 and Y12 differences

Build with the correct physical board. Keep the Linux device tree, official SPI BIN and official MCU assets aligned with that selection.

| Item | Y4 | Y12 |
| --- | --- | --- |
| Linux device tree | `sky1-y4.dtb` | `sky1-y12.dtb` |
| Network interface prefix | `y4` | `y12` |
| Fan mapping | Board fan 1–3 each corresponds to MCU FAN3–5. SoC FAN1 has its own thermal controls | Six socket tachometers and two SoC PWM groups |

See the [board API](board-api.md) for the current interface contract.

## Y12 slot mapping

| Physical slot | PCIe switch downstream device number | INA3221 address / channel | Measurement channel | Run LED GPIO offset |
| --- | --- | --- | --- | --- |
| 1 | 8 | 0x42 / 3 | 9 | 8 |
| 2 | 7 | 0x42 / 2 | 8 | 7 |
| 3 | 1 | 0x40 / 2 | 2 | 1 |
| 4 | 2 | 0x40 / 3 | 3 | 2 |
| 5 | 4 | 0x41 / 2 | 5 | 4 |
| 6 | 5 | 0x41 / 3 | 6 | 5 |
| 7 | 11 | 0x43 / 3 | 12 | 11 |
| 8 | 10 | 0x43 / 2 | 11 | 10 |
| 9 | 9 | 0x43 / 1 | 10 | 9 |
| 10 | 6 | 0x42 / 1 | 7 | 6 |
| 11 | 0 | 0x40 / 1 | 1 | 0 |
| 12 | 3 | 0x41 / 1 | 4 | 3 |
