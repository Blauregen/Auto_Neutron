from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from auto_neutron.fsd import FrameShiftDrive
from auto_neutron.ship import Ship

if sys.platform == "win32":
    from auto_neutron.journal import Journal
    from auto_neutron.windows.new_route_window_tabs import ExactTab, NeutronTab


def read_loadout(name: str) -> dict:
    """Read a loadout stripped of account, location and unrelated module data."""
    path = Path(__file__).parent / "fixtures" / f"{name}_loadout.json"
    return json.loads(path.read_text(encoding="utf8"))


class ShipTests(unittest.TestCase):
    """Check ship imports against journal ranges and Spansh drive data."""

    def test_mkii_constants_match_spansh(self) -> None:
        """Use the Mk II fuel curve and neutron multiplier from Spansh."""
        fsd = FrameShiftDrive.from_loadout_dict(read_loadout("caspian_explorer"))
        self.assertEqual(fsd.size_const, 2.5025)
        self.assertEqual(fsd.rating_const / 1000, 0.011)
        self.assertEqual(fsd.max_fuel_usage, 6.8)
        self.assertEqual(fsd.optimal_mass, 7528.039551)
        self.assertEqual(fsd.supercharge_multiplier, 6)

    def test_sco_constants_unchanged(self) -> None:
        """Keep the working Mandalay's standard SCO drive behaviour."""
        fsd = FrameShiftDrive.from_loadout_dict(read_loadout("mandalay"))
        self.assertEqual(fsd.size_const, 2.45)
        self.assertEqual(fsd.rating_const / 1000, 0.013)
        self.assertEqual(fsd.max_fuel_usage, 5.2)
        self.assertEqual(fsd.optimal_mass, 1997.5)
        self.assertEqual(fsd.supercharge_multiplier, 4)

    def test_standard_drive_unchanged(self) -> None:
        """Keep conventional and size 8 SCO drive constants unchanged."""
        for item, size, rating, max_fuel, optimal_mass in (
            ("int_hyperdrive_size5_class5", 2.45, 12, 5.0, 1050),
            ("int_hyperdrive_overcharge_size8_class5", 2.9, 13, 20.7, 4670),
        ):
            with self.subTest(item=item):
                fsd = FrameShiftDrive.from_loadout_dict(
                    {"Modules": [{"Slot": "FrameShiftDrive", "Item": item}]}
                )
                self.assertEqual(fsd.size_const, size)
                self.assertEqual(fsd.rating_const, rating)
                self.assertEqual(fsd.max_fuel_usage, max_fuel)
                self.assertEqual(fsd.optimal_mass, optimal_mass)
                self.assertEqual(fsd.supercharge_multiplier, 4)

    def test_full_tank_range_matches_journal_maximum(self) -> None:
        """Match each journal's independent maximum range after adding fuel and cargo."""
        for name in ("caspian_explorer", "mandalay"):
            loadout = read_loadout(name)
            ship = Ship.from_loadout(loadout)
            for cargo in (0, ship.max_cargo):
                with self.subTest(ship=name, cargo=cargo):
                    # The journal maximum uses only enough main fuel for one jump,
                    # an empty reserve and no cargo; the UI uses full fuel tanks.
                    expected = (loadout["MaxJumpRange"] - 10.5) * (
                        ship.unladen_mass + ship.fsd.max_fuel_usage
                    ) / (
                        ship.unladen_mass + ship.tank_size + ship.reserve_size + cargo
                    ) + 10.5
                    self.assertAlmostEqual(
                        ship.jump_range(cargo_mass=cargo), expected, delta=0.00001
                    )

    def test_engineering_does_not_change_base_drive(self) -> None:
        """Preserve engineering overrides without leaking them to other ships."""
        loadout = read_loadout("caspian_explorer")
        fsd_module = next(
            module
            for module in loadout["Modules"]
            if module["Slot"] == "FrameShiftDrive"
        )
        fsd_module["Engineering"]["Modifiers"].append(
            {"Label": "MaxFuelPerJump", "Value": 7.48}
        )
        engineered = FrameShiftDrive.from_loadout_dict(loadout)
        self.assertEqual(engineered.max_fuel_usage, 7.48)
        self.assertEqual(engineered.optimal_mass, 7528.039551)
        self.assertEqual(engineered.supercharge_multiplier, 6)

        del fsd_module["Engineering"]
        base = FrameShiftDrive.from_loadout_dict(loadout)
        self.assertEqual(base.max_fuel_usage, 6.8)
        self.assertEqual(base.optimal_mass, 4670)
        self.assertEqual(base.supercharge_multiplier, 6)

    def test_guardian_booster_is_imported(self) -> None:
        """Recognize the Guardian booster alongside the new hull and drive."""
        for name in ("caspian_explorer", "mandalay"):
            with self.subTest(ship=name):
                loadout = read_loadout(name)
                self.assertEqual(Ship.from_loadout(loadout).jump_range_boost, 10.5)
                without_booster = copy.deepcopy(loadout)
                without_booster["Modules"] = [
                    module
                    for module in without_booster["Modules"]
                    if "guardianfsdbooster" not in module["Item"]
                ]
                self.assertEqual(Ship.from_loadout(without_booster).jump_range_boost, 0)

    def test_switching_between_loadouts(self) -> None:
        """Update the same ship object when the tracked loadout changes."""
        ship = Ship()
        for name, multiplier, max_fuel in (
            ("caspian_explorer", 6, 6.8),
            ("mandalay", 4, 5.2),
            ("caspian_explorer", 6, 6.8),
        ):
            with self.subTest(ship=name):
                ship.update_from_loadout(read_loadout(name))
                self.assertEqual(ship.fsd.supercharge_multiplier, multiplier)
                self.assertEqual(ship.fsd.max_fuel_usage, max_fuel)


@unittest.skipUnless(sys.platform == "win32", "Plotter tabs depend on Windows APIs")
class PlotRequestTests(unittest.TestCase):
    """Check the real plotter request builders without making network requests."""

    def test_journal_parses_loadout_once(self) -> None:
        """Read each ship through the journal parser and emit its loadout signal."""
        for name in ("caspian_explorer", "mandalay"):
            with self.subTest(ship=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "Journal.test.log"
                path.write_text(json.dumps(read_loadout(name)) + "\n", encoding="utf8")
                journal = Journal(path)
                updates = []
                journal.loadout_sig.connect(updates.append)
                journal.parse()
                journal.parse()
                self.assertEqual(updates, [journal.ship])
                self.assertEqual(journal.ship.jump_range_boost, 10.5)
                self.assertGreater(journal.ship.jump_range(cargo_mass=0), 0)

    def test_neutron_uses_ship_supercharge_multiplier(self) -> None:
        """Send 6x for the Mk II and 4x for the working SCO drive."""
        for name, multiplier in (("caspian_explorer", 6), ("mandalay", 4)):
            with self.subTest(ship=name):
                tab = SimpleNamespace(
                    _journal=SimpleNamespace(
                        ship=Ship.from_loadout(read_loadout(name))
                    ),
                    efficiency_spin=SimpleNamespace(value=80),
                    range_spin=SimpleNamespace(value=85),
                    source_edit=SimpleNamespace(text="Sol"),
                    target_edit=SimpleNamespace(text="Colonia"),
                )
                params = NeutronTab._request_params(tab)
                self.assertEqual(params["supercharge_multiplier"], multiplier)
                self.assertEqual(params["range"], 85)
                self.assertEqual(params["efficiency"], 80)
                self.assertEqual(params["from"], "Sol")
                self.assertEqual(params["to"], "Colonia")

    def test_neutron_without_ship_uses_standard_multiplier(self) -> None:
        """Keep manually entered ranges usable before a loadout is available."""
        for journal in (None, SimpleNamespace(ship=None)):
            with self.subTest(journal=journal):
                tab = SimpleNamespace(
                    _journal=journal,
                    efficiency_spin=SimpleNamespace(value=80),
                    range_spin=SimpleNamespace(value=50),
                    source_edit=SimpleNamespace(text="Sol"),
                    target_edit=SimpleNamespace(text="Colonia"),
                )
                self.assertEqual(
                    NeutronTab._request_params(tab)["supercharge_multiplier"], 4
                )

    def test_galaxy_uses_drive_fuel_curve_and_supercharge(self) -> None:
        """Send each journal ship's fuel curve, engineering and boost to Spansh."""
        for name, power, multiplier, supercharge in (
            ("caspian_explorer", 2.5025, 0.011, 6),
            ("mandalay", 2.45, 0.013, 4),
        ):
            with self.subTest(ship=name):
                ship = Ship.from_loadout(read_loadout(name))
                tab = SimpleNamespace(
                    _journal=SimpleNamespace(ship=ship),
                    use_clipboard_checkbox=SimpleNamespace(checked=False),
                    source_edit=SimpleNamespace(text="Sol"),
                    target_edit=SimpleNamespace(text="Colonia"),
                    is_supercharged_checkbox=SimpleNamespace(checked=False),
                    supercarge_checkbox=SimpleNamespace(checked=True),
                    fsd_injections_checkbox=SimpleNamespace(checked=False),
                    exclude_secondary_checkbox=SimpleNamespace(checked=True),
                    cargo_slider=SimpleNamespace(value=8),
                )
                params = ExactTab._request_params(tab)
                self.assertEqual(params["fuel_power"], power)
                self.assertEqual(params["fuel_multiplier"], multiplier)
                self.assertEqual(params["supercharge_multiplier"], supercharge)
                self.assertEqual(params["max_fuel_per_jump"], ship.fsd.max_fuel_usage)
                self.assertEqual(params["optimal_mass"], ship.fsd.optimal_mass)
                self.assertEqual(params["range_boost"], 10.5)
                self.assertEqual(params["tank_size"], ship.tank_size)
                self.assertEqual(
                    params["base_mass"], ship.unladen_mass + ship.reserve_size
                )
                self.assertEqual(params["internal_tank_size"], ship.reserve_size)
                self.assertEqual(params["cargo"], 8)


if __name__ == "__main__":
    unittest.main()
