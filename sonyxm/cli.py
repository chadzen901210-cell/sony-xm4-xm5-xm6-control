"""Command line interface: python -m sonyxm <command>."""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time

from . import __version__, bluetooth
from .bluetooth import BluetoothError
from .connection import DEFAULT_CONNECT_TIMEOUT, Log, ResponseTimeout, SessionError, connect
from .headphones import Change, Headphones, describe_equalizer, describe_noise_control
from .headtracker import HeadTrackerError
from .models import MODELS, find_model
from .protocol import (
    AMBIENT_LEVEL_MAX,
    AMBIENT_LEVEL_MIN,
    EQ_BAND_NAMES,
    EQ_BAND_NAMES_10,
    EQ_BAND_SLOTS,
    EQ_STEP_MAX,
    EQ_STEP_MAX_10,
    EQ_STEP_MIN,
    EqPreset,
    Battery,
    ChargingStatus,
    Command,
    Frame,
    MessageType,
    NcAsmMode,
    NcAsmState,
    PayloadError,
    ProtocolError,
    parse_battery,
    parse_ncasm,
)

_CHARGING_LABELS = {
    ChargingStatus.NOT_CHARGING: "",
    ChargingStatus.CHARGING: " (charging)",
    ChargingStatus.CHARGED: " (fully charged)",
}


def _make_logger(enabled: bool) -> Log | None:
    if not enabled:
        return None
    start = time.monotonic()

    def log(text: str) -> None:
        print(f"[{time.monotonic() - start:7.3f}] {text}", file=sys.stderr, flush=True)

    return log


def _open(args: argparse.Namespace, log: Log | None) -> Headphones:
    conn, device = connect(args.address, timeout=args.timeout, log=log)
    return Headphones(conn, log, find_model(device.name))


def cmd_devices(args: argparse.Namespace, log: Log | None) -> int:
    devices = bluetooth.list_paired_devices()
    if not devices:
        print("No paired Bluetooth devices.")
        return 0
    width = max(len(d.name) for d in devices)
    for d in devices:
        marker = "*" if find_model(d.name) else " "
        state = "connected" if d.connected else ""
        print(f"{marker} {d.name:<{width}}  {d.address}  {state}".rstrip())
    return 0


def cmd_connect(args: argparse.Namespace, log: Log | None) -> int:
    conn, device = connect(args.address, timeout=args.timeout, log=log)
    with conn:
        print(f"Connected to {device.name} ({device.address})")
        hp = Headphones(conn, log, find_model(device.name))
        version = hp.handshake()
        print(f"Handshake OK, protocol {version}")
        print(format_model(hp))
    return 0


def format_battery(battery: Battery) -> str:
    label = _CHARGING_LABELS.get(battery.charging)
    if label is None:
        label = f" (charging status {int(battery.charging)})"
    return f"Battery: {battery.level}%{label}"


def format_ncasm(state: NcAsmState) -> str:
    return f"Noise control: {describe_noise_control(state)}"


def format_model(hp: Headphones) -> str:
    model = hp.model
    note = "" if model.tested else " (not yet tested on these headphones by this project)"
    return f"Model: {model.name}, protocol {hp.version}{note}"


def cmd_battery(args: argparse.Namespace, log: Log | None) -> int:
    hp = _open(args, log)
    with hp.conn:
        hp.handshake()
        battery = hp.battery()
    print(format_battery(battery))
    return 0


def cmd_status(args: argparse.Namespace, log: Log | None) -> int:
    hp = _open(args, log)
    with hp.conn:
        hp.handshake()
        if args.probe:
            # Diagnostic: ask with every sub-type and show each answer
            print(format_model(hp))
            for subtype in hp.model.ncasm_subtypes:
                try:
                    result = format_ncasm(hp.query_noise_control(subtype))
                except (ResponseTimeout, PayloadError) as e:
                    result = str(e)
                print(f"sub-type 0x{subtype:02x}: {result}")
        else:
            print(format_ncasm(hp.noise_control()))

        if args.watch:
            print("Watching for changes; press Ctrl+C to stop.", flush=True)
            try:
                hp.conn.listen(lambda frame: _print_notification(frame, args.probe))
            except KeyboardInterrupt:
                pass
    return 0


def _print_notification(frame: Frame, show_subtype: bool) -> None:
    if frame.type != MessageType.COMMAND_1 or not frame.payload:
        return
    stamp = time.strftime("%H:%M:%S")
    try:
        if frame.payload[0] == Command.NCASM_NOTIFY:
            subtype, state = parse_ncasm(frame.payload)
            suffix = f" (sub-type 0x{subtype:02x})" if show_subtype else ""
            print(f"[{stamp}] {format_ncasm(state)}{suffix}", flush=True)
        elif frame.payload[0] in (Command.BATTERY_NOTIFY, Command.BATTERY_NOTIFY_V1):
            print(f"[{stamp}] {format_battery(parse_battery(frame.payload))}", flush=True)
    except PayloadError as e:
        print(f"[{stamp}] unexpected notification: {e}", flush=True)


def _run_change(args: argparse.Namespace, log: Log | None, change: Change) -> int:
    hp = _open(args, log)
    with hp.conn:
        hp.handshake()
        state = hp.change_noise_control(change)
    print(format_ncasm(state))
    return 0


def cmd_nc(args: argparse.Namespace, log: Log | None) -> int:
    mode = {"on": NcAsmMode.NOISE_CANCELLING, "off": NcAsmMode.OFF, "wind": NcAsmMode.WIND}[args.state]
    return _run_change(args, log, lambda current: dataclasses.replace(current, mode=mode))


def cmd_ambient(args: argparse.Namespace, log: Log | None) -> int:
    def change(current: NcAsmState) -> NcAsmState:
        return dataclasses.replace(
            current,
            mode=NcAsmMode.AMBIENT,
            focus_on_voice=current.focus_on_voice if args.voice is None else args.voice,
            ambient_level=current.ambient_level if args.level is None else args.level,
            auto_ambient=current.auto_ambient if args.auto is None else args.auto,
        )

    return _run_change(args, log, change)


def cmd_eq(args: argparse.Namespace, log: Log | None) -> int:
    hp = _open(args, log)
    with hp.conn:
        hp.handshake()
        if args.eq_action == "preset":
            eq = hp.set_equalizer_preset(EqPreset[args.name.upper()])
        elif args.eq_action == "bands":
            slot = EqPreset[args.slot.upper()] if args.slot else None
            clear_bass = args.clear_bass
            if len(args.values) == len(EQ_BAND_NAMES) and clear_bass is None:
                clear_bass = 0
            eq = hp.set_equalizer_bands(clear_bass, tuple(args.values), slot)
        else:
            eq = hp.equalizer()
    print(f"Equalizer: {describe_equalizer(eq)}")
    return 0


def _eq_step(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if not EQ_STEP_MIN <= value <= EQ_STEP_MAX:
        raise argparse.ArgumentTypeError(f"must be between {EQ_STEP_MIN} and {EQ_STEP_MAX}")
    return value


def cmd_head(args: argparse.Namespace, log: Log | None) -> int:
    from . import headtracker

    if args.repair:
        return _head_repair(args)
    tracker = headtracker.open_head_tracker(log)
    orientation = headtracker.Orientation()
    print("Head tracking. Turn your head; press Ctrl+C to stop.", flush=True)
    deadline = time.monotonic() + args.seconds if args.seconds else None
    started = time.monotonic()
    count = 0
    last_print = 0.0
    with tracker:
        try:
            while deadline is None or time.monotonic() < deadline:
                sample = tracker.read(timeout=1.0)
                if sample is None:
                    print("\n(no data from the headphones for 1 s)", flush=True)
                    continue
                angles = orientation.update(sample)
                count += 1
                now = time.monotonic()
                if now - last_print >= 0.1:
                    rate = count / (now - started)
                    print(f"\ryaw {angles.yaw:+7.1f}°   pitch {angles.pitch:+6.1f}°   roll {angles.roll:+6.1f}°"
                          f"   ({rate:4.1f} samples/s)", end="", flush=True)
                    last_print = now
        except KeyboardInterrupt:
            pass
    print()
    return 0


def _check_band_count(args: argparse.Namespace) -> None:
    if len(args.values) not in (len(EQ_BAND_NAMES), len(EQ_BAND_NAMES_10)):
        raise argparse.ArgumentTypeError(
            f"give {len(EQ_BAND_NAMES)} values ({', '.join(EQ_BAND_NAMES)}) or, on the WH-1000XM6, "
            f"{len(EQ_BAND_NAMES_10)} values ({', '.join(EQ_BAND_NAMES_10)})"
        )


def _head_repair(args: argparse.Namespace) -> int:
    from . import headtracker

    if not headtracker.is_admin():
        print("The repair needs administrator rights. Windows will ask for permission; "
              "the repair then runs in a new window.", flush=True)
        arguments = ["head", "--repair", "--pause"]
        if args.address:
            arguments += ["--address", args.address]
        code = headtracker.run_elevated(arguments, wait=True)
        print("Repair finished. Now run: sonyxm head" if code == 0 else "The repair did not succeed (see its window).")
        return code
    device = bluetooth.pick_headphones(bluetooth.list_paired_devices(), args.address)
    code = 0
    try:
        headtracker.repair(device.address, lambda text: print(text, flush=True))
        print("Done. Head tracking should work now: run `sonyxm head`.")
    except (HeadTrackerError, BluetoothError) as e:
        print(f"error: {e}")
        code = 1
    if args.pause:
        input("Press Enter to close this window…")
    return code


def cmd_gui(args: argparse.Namespace, log: Log | None) -> int:
    from .webui import run

    return run(address=args.address, timeout=args.timeout, log=log)


def _ambient_level(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {text!r}") from None
    if not AMBIENT_LEVEL_MIN <= value <= AMBIENT_LEVEL_MAX:
        raise argparse.ArgumentTypeError(f"must be between {AMBIENT_LEVEL_MIN} and {AMBIENT_LEVEL_MAX}")
    return value


def _common_options(nested: bool = False) -> argparse.ArgumentParser:
    """Options every command takes. Nested copies have no defaults, so they never
    overwrite a value given before the sub-command (an argparse quirk)."""

    def default(value: object) -> object:
        return argparse.SUPPRESS if nested else value

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--address", default=default(None),
                        help="Bluetooth address of the headphones (default: find by name)")
    common.add_argument(
        "--timeout", type=float, default=default(DEFAULT_CONNECT_TIMEOUT),
        help=f"connection timeout in seconds (default: {DEFAULT_CONNECT_TIMEOUT:g})",
    )
    common.add_argument("--debug", action="store_true", default=default(False),
                        help="print raw bytes sent and received")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    nested = _common_options(nested=True)

    parser = argparse.ArgumentParser(
        prog="sonyxm", description=f"Control Sony {', '.join(m.name for m in MODELS)} headphones."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("devices", parents=[common], help="list paired Bluetooth devices").set_defaults(
        func=cmd_devices
    )
    sub.add_parser("battery", parents=[common], help="show the battery level").set_defaults(
        func=cmd_battery
    )
    status = sub.add_parser("status", parents=[common], help="show the noise cancelling / ambient sound state")
    status.add_argument("--watch", action="store_true", help="keep listening and print changes until Ctrl+C")
    status.add_argument(
        "--probe", action="store_true", help="diagnostic: query with every NC sub-type and show each answer"
    )
    status.set_defaults(func=cmd_status)
    nc = sub.add_parser("nc", parents=[common], help="turn noise cancelling on, or noise control off")
    nc.add_argument(
        "state", choices=["on", "off", "wind"],
        help="on: noise cancelling; off: noise cancelling and ambient sound both off; "
             "wind: wind noise reduction (WH-1000XM4 only)",
    )
    nc.set_defaults(func=cmd_nc)
    ambient = sub.add_parser("ambient", parents=[common], help="switch to ambient sound mode")
    ambient.add_argument(
        "level", nargs="?", type=_ambient_level,
        help=f"ambient sound level {AMBIENT_LEVEL_MIN}-{AMBIENT_LEVEL_MAX} (default: keep the current level)",
    )
    ambient.add_argument(
        "--voice", action=argparse.BooleanOptionalAction, default=None,
        help="focus on voice (default: keep the current setting)",
    )
    ambient.add_argument(
        "--auto", action=argparse.BooleanOptionalAction, default=None,
        help="let the level follow the surroundings (WH-1000XM6 only; default: keep the current setting)",
    )
    ambient.set_defaults(func=cmd_ambient)
    eq = sub.add_parser("eq", parents=[common], help="show or change the equalizer")
    eq_sub = eq.add_subparsers(dest="eq_action", metavar="action")
    eq_preset = eq_sub.add_parser("preset", parents=[nested], help="choose a preset")
    eq_preset.add_argument("name", choices=[p.name.lower() for p in EqPreset])
    eq_bands = eq_sub.add_parser(
        "bands", parents=[nested],
        help=f"set the bands by hand ({EQ_STEP_MIN}..{EQ_STEP_MAX} each; {-EQ_STEP_MAX_10}..{EQ_STEP_MAX_10} "
             f"with 10 bands)",
    )
    eq_bands.add_argument("values", nargs="+", type=_eq_step, metavar="N",
                          help=f"values for {', '.join(EQ_BAND_NAMES)} "
                               f"(WH-1000XM6: {', '.join(EQ_BAND_NAMES_10)})")
    eq_bands.add_argument("--clear-bass", type=_eq_step, default=None,
                          help="Clear Bass (default: 0; not on the WH-1000XM6)")
    eq_bands.add_argument(
        "--slot", choices=[s.name.lower() for s in EQ_BAND_SLOTS],
        help="where to store them (default: the current Custom 1/2, otherwise Manual)",
    )
    eq_bands.set_defaults(check=_check_band_count)
    eq.set_defaults(func=cmd_eq)
    head = sub.add_parser("head", parents=[common], help="show live head tracking angles")
    head.add_argument("--seconds", type=float, help="stop after this many seconds (default: until Ctrl+C)")
    head.add_argument(
        "--repair", action="store_true",
        help="make Windows set up the head tracking sensor (asks for administrator rights)",
    )
    head.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    head.set_defaults(func=cmd_head)
    sub.add_parser("gui", parents=[common], help="open a window with all the controls").set_defaults(
        func=cmd_gui
    )
    sub.add_parser(
        "connect", parents=[common], help="connect and run the handshake (changes nothing)"
    ).set_defaults(func=cmd_connect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "check"):
        try:
            args.check(args)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))
    log = _make_logger(args.debug)
    try:
        return args.func(args, log)
    except HeadTrackerError as e:
        print(f"error: {e}", file=sys.stderr)
        if args.command == "head" and not args.repair:
            print("To fix it, run: sonyxm head --repair", file=sys.stderr)
    except (BluetoothError, SessionError, ProtocolError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
    except OSError as e:
        print(f"error: connection problem: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    return 1
