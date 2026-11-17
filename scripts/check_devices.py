"""Phase 0 of docs/HARDWARE_AWARE_INFERENCE_PLAN.md: every unit on this laptop AJ could run a model on,
each marked usable or not, with the reason. No changes to AJ.

    .venv\\Scripts\\python.exe scripts\\check_devices.py

Writes data/bench/placement/devices.json. Run it plugged in: it asks nvidia-smi about the RTX
4050, which wakes the card (scripts/hwprobe.py). The 780M is checked through the Vulkan build of
llama-server (scripts\\fetch_models.ps1 -Vulkan); without it, the 780M is "not checked".

The NPU: on the Ryzen 7 7840HS it is a PCI device of AMD's (1022:1502, "AMD IPU Device", or
1022:17F0 on later chips) that Windows lists even with no driver. On this laptop it isn't there at
all, not even hidden: Lenovo's firmware doesn't expose it, and the owner confirmed on 2026-10-26
that it has no NPU. So it is reported as not present, with that evidence, and nothing depends on it.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hwprobe  # noqa: E402

OUT = hwprobe.ROOT / "data" / "bench" / "placement" / "devices.json"
_NPU_IDS = ("VEN_1022&DEV_1502", "VEN_1022&DEV_17F0")


def find(devices: list[dict], *, ids: tuple[str, ...] = (), name: str = "", classes: tuple[str, ...] = ()) -> list[dict]:
    """The devices matching any of a hardware id, a word in the name or a device class."""
    out = []
    for d in devices:
        instance, friendly, kind = (d.get("InstanceId") or "").upper(), d.get("FriendlyName") or "", d.get("Class") or ""
        if any(i in instance for i in ids) or (name and name.lower() in friendly.lower()) or kind in classes:
            out.append(d)
    return out


def driver_of(drivers: list[dict], word: str) -> dict:
    return next((d for d in drivers if word.lower() in (d.get("DeviceName") or "").lower()), {})


def check(devices: list[dict], drivers: list[dict], vulkan: list[dict], nvidia: dict, bios: str, cpu: str,
          threads: int) -> dict:
    """The verdict for each unit, from readings already taken (so it can be tested)."""
    units = {}

    rtx = find(devices, name="RTX 4050")
    rtx_on = [d for d in rtx if d.get("Present")]
    units["rtx4050"] = {
        "kind": "CUDA (llama.cpp CUDA build) and Vulkan",
        "usable": bool(rtx_on and nvidia),
        "reason": ("driver loaded, nvidia-smi answers" if rtx_on and nvidia else
                   "not present" if not rtx_on else "nvidia-smi gave no answer"),
        "driver": driver_of(drivers, "NVIDIA"), "memory_mb": nvidia.get("total_mb"),
        "vulkan": next((v for v in vulkan if "NVIDIA" in v["name"]), None),
    }

    radeon = [d for d in find(devices, name="Radeon") if d.get("Present") and d.get("Class") == "Display"]
    radeon_vk = next((v for v in vulkan if "Radeon" in v["name"]), None)
    units["radeon780m"] = {
        "kind": "Vulkan (llama.cpp Vulkan build); its memory is borrowed from system RAM",
        "usable": bool(radeon and radeon_vk),
        "reason": ("listed by the Vulkan build" if radeon and radeon_vk else
                   "not listed by Windows: is graphics set to Hybrid in Lenovo Vantage?" if not radeon else
                   "Vulkan build missing: scripts\\fetch_models.ps1 -Vulkan" if not vulkan else
                   "the Vulkan build doesn't list it"),
        "hybrid_mode": bool(radeon), "driver": driver_of(drivers, "Radeon"), "vulkan": radeon_vk,
    }

    units["cpu"] = {"kind": "CPU (llama.cpp, ONNX Runtime)", "usable": True, "reason": "always", "name": cpu,
                    "threads": threads}

    npu = find(devices, ids=_NPU_IDS, classes=("ComputeAccelerator", "NeuralProcessor"))
    units["npu"] = {
        "kind": "XDNA NPU (Vitis AI execution provider)",
        "usable": False if not npu else None,
        "reason": (f"not present: no AMD IPU PCI device ({', '.join(_NPU_IDS)}) among Windows' devices, hidden ones "
                   f"included, on BIOS {bios}. Lenovo's firmware doesn't expose it; the owner confirmed on "
                   f"2026-10-26 that this laptop has no NPU." if not npu else
                   "listed by Windows: run AMD's NPU smoke test (Phase 0) before calling it usable"),
        "devices": [{k: d.get(k) for k in ("FriendlyName", "Status", "InstanceId")} for d in npu],
    }
    return units


def main() -> int:
    devices = hwprobe.pnp_devices()
    drivers = hwprobe.display_drivers()
    vulkan = hwprobe.vulkan_devices()
    nvidia = hwprobe.nvidia_smi()
    total, free = hwprobe.ram_mb()
    battery = hwprobe.battery()
    gpu_ids = [d["InstanceId"] for d in find(devices, name="RTX 4050") if d.get("Present")]
    report = {
        "when": time.strftime("%Y-%m-%d %H:%M"),
        "bios": hwprobe.bios_version(),
        "windows": sys.getwindowsversion().build,
        "ram_mb": {"total": total, "free": free},
        "battery": None if battery is None else {"on_ac": battery.on_ac, "watts": battery.watts,
                                                 "capacity_mwh": battery.capacity_mwh},
        "rtx4050_power_state": hwprobe.power_state(gpu_ids[0]) if gpu_ids else None,
        "units": check(devices, drivers, vulkan, nvidia, hwprobe.bios_version(), hwprobe.cpu_name(), os.cpu_count() or 0),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print(f"BIOS {report['bios']}, Windows build {report['windows']}, RAM {total} MB ({free} MB free)")
    if battery:
        print(f"Battery: {'plugged in' if battery.on_ac else f'on battery, {battery.watts} W'}, "
              f"{battery.capacity_mwh} mWh left")
    print(f"RTX 4050 power state now: {report['rtx4050_power_state']} (after nvidia-smi, so likely awake)\n")
    for key, unit in report["units"].items():
        verdict = {True: "usable", False: "NOT usable", None: "unknown"}[unit["usable"]]
        print(f"{key:12} {verdict:11} {unit['reason']}")
    print(f"\nWritten to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
