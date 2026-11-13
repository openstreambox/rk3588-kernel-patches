"""Check that 0052 leaves the Rock 5B+ M.2 B-key card off until RESET# is high.

Two layers:

* The default unittest run reads the ceralive/ source patch only. It needs no
  kernel tree and no tools, so it runs everywhere the other fixtures run.
* ``--tree <applied kernel tree>`` compiles rk3588-rock-5b-plus.dtb from that
  tree with cpp and dtc, the way kbuild does, reads it back with fdtget, and checks the
  properties the regulator core and regulator-fixed will actually see. apply.sh
  calls this mode after ``git am``.

The intended boot order, which these properties produce on this base:

1. The gpio2 bank probes and the RESET# hog drives GPIO2_B3 physically high.
2. regulator-fixed probes vcc3v3_wwan_pwr. Without regulator-boot-on it
   requests its enable GPIO (GPIO2_B1, FULL_CARD_POWER_OFF#) LOW, so a card the
   bootloader had already powered is switched off while vcc3v3_4g stays on.
3. regulator-always-on makes the core enable it at registration. The core's
   off-on delay counts from boot for an always-on supply that was never
   disabled (last_off is zero), so the enable waits until off-on-delay-us
   after boot and the card cold-starts with RESET# already high.
"""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "ceralive/0052-rk3588-rock-5b-plus-wwan-cold-start.patch"
DTS = "arch/arm64/boot/dts/rockchip/rk3588-rock-5b-plus.dts"

# The module manual asks for power to stay applied for at least two seconds after
# FULL_CARD_POWER_OFF# is asserted. The enable GPIO is first driven low when the
# regulator probes, which has been observed at about 0.34 s after boot; one more
# second covers a slower or deferred probe. The delay counts from boot, so it
# must cover both.
MIN_OFF_US = 2_000_000
PROBE_ALLOWANCE_US = 1_000_000


def patch_lines() -> list[str]:
    return PATCH.read_text().splitlines()


def touched_files(lines: list[str]) -> list[str]:
    return [line[len("+++ b/"):] for line in lines if line.startswith("+++ b/")]


def body(lines: list[str], prefix: str) -> list[str]:
    return [line[1:].strip() for line in lines
            if line.startswith(prefix) and not line.startswith(prefix * 3)]


def delay_us(text: str) -> int:
    match = re.search(r"off-on-delay-us = <(0x[0-9a-f]+|[0-9]+)>;", text)
    if match is None:
        raise AssertionError("off-on-delay-us is missing")
    return int(match.group(1), 0)


class SourcePatch(unittest.TestCase):
    def test_touches_only_the_rock_5b_plus_board_file(self) -> None:
        self.assertEqual(touched_files(patch_lines()), [DTS])

    def test_hunk_is_inside_the_wwan_power_regulator(self) -> None:
        context = [line[1:].strip() for line in patch_lines() if line.startswith(" ")]
        self.assertIn('regulator-name = "vcc3v3_wwan_pwr";', context)
        self.assertIn("vin-supply = <&vcc3v3_4g>;", context)

    def test_removes_boot_on_and_nothing_else(self) -> None:
        self.assertEqual(body(patch_lines(), "-"), ["regulator-boot-on;"])

    def test_keeps_always_on(self) -> None:
        lines = patch_lines()
        kept = [line[1:].strip() for line in lines if line.startswith((" ", "+"))]
        self.assertIn("regulator-always-on;", kept)

    def test_delay_covers_the_documented_off_time_and_the_probe(self) -> None:
        added = "\n".join(body(patch_lines(), "+"))
        self.assertGreaterEqual(delay_us(added), MIN_OFF_US + PROBE_ALLOWANCE_US)


WWAN = "/regulator-vcc3v3-wwan"
RAIL = "/regulator-vcc3v3-4g"
RESET_HOG = "/pinctrl/gpio@fec30000/wwan-reset-n-hog"  # gpio2


def compile_dtb(tree: Path, out_dir: Path) -> Path:
    for tool in ("cpp", "dtc", "fdtget"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is required for --tree (device-tree-compiler, cpp)")
    pre = out_dir / "rock-5b-plus.dts.pre"
    dtb = out_dir / "rock-5b-plus.dtb"
    subprocess.run(
        ["cpp", "-nostdinc", "-undef", "-D__DTS__", "-x", "assembler-with-cpp",
         "-I", str(tree / "include"), "-I", str(tree / "arch/arm64/boot/dts"),
         "-I", str(tree / "arch/arm64/boot/dts/rockchip"),
         "-I", str(tree / "scripts/dtc/include-prefixes"),
         str(tree / DTS), "-o", str(pre)],
        check=True)
    subprocess.run(["dtc", "-q", "-@", "-I", "dts", "-O", "dtb", "-o", str(dtb), str(pre)],
                   check=True)
    return dtb


def props(dtb: Path, path: str) -> set[str]:
    out = subprocess.run(["fdtget", "-p", str(dtb), path],
                         check=True, capture_output=True, text=True).stdout
    return set(out.split())


def cells(dtb: Path, path: str, prop: str) -> list[int]:
    # Read as unsigned cells explicitly: decompiled DTS guesses types, and a
    # 4,000,000 cell decompiles as a string.
    out = subprocess.run(["fdtget", "-t", "u", str(dtb), path, prop],
                         capture_output=True, text=True)
    return [int(v) for v in out.stdout.split()] if out.returncode == 0 else []


def check_tree(tree: Path) -> None:
    failures = []

    def expect(ok: bool, what: str) -> None:
        print(("  ok      " if ok else "  FAILED  ") + what)
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        dtb = compile_dtb(tree, Path(tmp))
        wwan = props(dtb, WWAN)
        expect("regulator-boot-on" not in wwan,
               "vcc3v3_wwan_pwr has no regulator-boot-on (enable GPIO requested low)")
        expect("regulator-always-on" in wwan, "vcc3v3_wwan_pwr stays regulator-always-on")
        delay = cells(dtb, WWAN, "off-on-delay-us")
        expect(len(delay) == 1 and delay[0] >= MIN_OFF_US + PROBE_ALLOWANCE_US,
               f"vcc3v3_wwan_pwr off-on-delay-us = {delay} >= {MIN_OFF_US + PROBE_ALLOWANCE_US}")
        gpio = cells(dtb, WWAN, "gpios")
        expect(len(gpio) == 3 and gpio[1:] == [9, 0],
               "vcc3v3_wwan_pwr enable is still GPIO2_B1, active high")

        rail = props(dtb, RAIL)
        expect({"regulator-boot-on", "regulator-always-on"} <= rail,
               "vcc3v3_4g (shared with onboard Wi-Fi) is unchanged")

        hog = props(dtb, RESET_HOG)
        expect(cells(dtb, RESET_HOG, "gpios") == [11, 1] and {"output-low", "gpio-hog"} <= hog,
               "RESET# hog still drives GPIO2_B3 high (active-low, output-low)")

    if failures:
        raise SystemExit(f"0052 DTB check failed: {len(failures)} item(s)")
    print("  ok      0052 Rock 5B+ B-key cold-start properties")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--tree":
        check_tree(Path(sys.argv[2]).resolve())
        sys.exit(0)
    unittest.main()
