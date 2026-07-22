#!/usr/bin/env python3
"""midea — an interactive CLI for a Midea (NetHome Plus) WiFi split AC.

Launch it once and type commands:

    on / off              power the unit on or off
    temp 23               set target temperature
    mode cool             set mode (cool|heat|auto|dry|fan)
    fan auto              set fan speed (auto|low|medium|high|max|silent)
    status                show a table of the current state
    chart [hours]         plot outdoor / home / target temp over time (default 6h)
    poll 30               change the background sampling interval (seconds)
    help                  list commands
    quit                  exit

On first run it discovers the unit on your LAN and saves its
ip/id/token/key to config.json next to this script.
"""

import asyncio
import atexit
import csv
import os
import re
import readline  # noqa: F401 — importing it gives input() arrow-key history & line editing
import sys
from datetime import datetime, timedelta
from pathlib import Path

import plotext as plt
from rich.console import Console
from rich.table import Table

from msmart.device import AirConditioner as AC
from msmart.discover import Discover

# We store config/history next to the script so the tool is self-contained.
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
HISTORY_PATH = HERE / "history.csv"
HISTORY_FIELDS = ["timestamp", "outdoor", "indoor", "target", "power", "mode"]
# Where we persist the interactive command history (for up/down recall across runs).
CMD_HISTORY_PATH = HERE / ".midea_history"

console = Console()


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict | None:
    import json

    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (ValueError, OSError) as e:
        console.print(f"[red]Could not read {CONFIG_PATH.name}: {e}[/red]")
        return None


def save_config(cfg: dict) -> None:
    import json

    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    console.print(f"[green]Saved configuration to {CONFIG_PATH.name}[/green]")


def _ask(label: str, default: str, env_key: str) -> str:
    """Read a setup value from env var, else prompt (only if we have a TTY)."""
    val = os.environ.get(env_key)
    if val is not None:
        return val.strip()
    if sys.stdin.isatty():
        try:
            return input(label).strip() or default
        except EOFError:
            return default
    return default


# msmart's built-in NetHome Plus account only ships cloud credentials for these
# regions (see msmart/cloud.py). EU/UK users want DE; SEA/Korea want KR. With
# your *own* account the region is ignored, so any of these is fine then.
BUILTIN_CLOUD_REGIONS = {"US", "DE", "KR"}
REGION_ALIASES = {
    "US": "US", "USA": "US", "NA": "US",
    "DE": "DE", "EU": "DE", "EUROPE": "DE", "UK": "DE", "GERMANY": "DE",
    "KR": "KR", "KOREA": "KR", "ASIA": "KR", "SEA": "KR",
}


async def setup_device() -> dict:
    """Discover a Midea AC on the LAN and return a config dict for it."""
    console.print(
        "\n[bold]First-time setup[/bold] — discovering Midea units on your network.\n"
        "Newer units need a one-time cloud handshake to fetch the local key.\n"
        "Press Enter to try the built-in default account first; if that finds\n"
        "the unit but cannot get a key, re-run and enter your NetHome Plus login\n"
        "(or set NETHOME_ACCOUNT / NETHOME_PASSWORD / AC_REGION env vars).\n"
    )
    region = _ask(
        "Region for the built-in account [US/DE/KR] (default US; EU/UK → DE): ",
        "US", "AC_REGION",
    ).strip().upper()
    account = _ask("NetHome Plus email (blank = built-in): ", "", "NETHOME_ACCOUNT")
    password = ""
    if account:
        password = _ask("NetHome Plus password: ", "", "NETHOME_PASSWORD")

    # The built-in account only has keys for US/DE/KR. Map common aliases (EU→DE,
    # etc.) and, if it's still unrecognised, fall back to US instead of letting
    # msmart raise "Unknown cloud region" deep inside discovery. With your own
    # account msmart ignores the region, so we leave it untouched in that case.
    if not account:
        mapped = REGION_ALIASES.get(region, region)
        if mapped not in BUILTIN_CLOUD_REGIONS:
            console.print(
                f"[yellow]No built-in account for region {region!r} "
                f"(supported: {', '.join(sorted(BUILTIN_CLOUD_REGIONS))}). "
                f"Falling back to US.[/yellow]\n"
                "[dim]For other regions, re-run and enter your own NetHome Plus "
                "login.[/dim]"
            )
            mapped = "US"
        region = mapped

    console.print("\n[cyan]Scanning the LAN (this takes a few seconds)…[/cyan]")
    try:
        devices = await Discover.discover(
            timeout=10,
            region=region,
            account=account or None,
            password=password or None,
        )
    except ValueError as e:
        # e.g. an unsupported cloud region slipping through with a custom account.
        console.print(
            f"[red]Discovery failed: {e}[/red]\n"
            "If this is a region problem, re-run and pick US, DE, or KR — or "
            "enter your own NetHome Plus account (which works for any region)."
        )
        sys.exit(1)
    acs = [d for d in devices if isinstance(d, AC)]
    if not acs:
        console.print(
            "[red]No air conditioner found.[/red] Make sure this computer is on the "
            "same WiFi/subnet as the AC, and that the unit is powered."
        )
        sys.exit(1)

    if len(acs) == 1:
        device = acs[0]
    else:
        console.print("\nMultiple units found:")
        for i, d in enumerate(acs):
            console.print(f"  [{i}] {d.name or 'AC'}  ip={d.ip}  id={d.id}")
        idx = int(_ask("Pick one [0]: ", "0", "AC_PICK") or "0")
        device = acs[idx]

    if device.token is None or device.key is None:
        console.print(
            "[yellow]Found the unit but could not retrieve its local key.[/yellow]\n"
            "Re-run setup and enter your own NetHome Plus account credentials."
        )

    cfg = {
        "ip": device.ip,
        "id": int(device.id),
        "port": device.port,
        "token": device.token,
        "key": device.key,
        "name": device.name or "AC",
        "poll_interval": 60,
    }
    save_config(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Device connection
# --------------------------------------------------------------------------- #
async def connect(cfg: dict) -> AC:
    device = AC(ip=cfg["ip"], device_id=int(cfg["id"]), port=cfg.get("port", 6444))
    if cfg.get("token") and cfg.get("key"):
        await device.authenticate(cfg["token"], cfg["key"])
    await device.get_capabilities()
    await device.refresh()
    return device


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def record_sample(device: AC) -> None:
    """Append the current readings to history.csv."""
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "outdoor": device.outdoor_temperature,
        "indoor": device.indoor_temperature,
        "target": device.target_temperature,
        "power": int(bool(device.power_state)),
        "mode": enum_name(device.operational_mode) if device.operational_mode else "",
    }
    new_file = not HISTORY_PATH.exists()
    with HISTORY_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def init_command_history() -> None:
    """Load past commands so up/down recalls them, and save on exit."""
    try:
        readline.read_history_file(CMD_HISTORY_PATH)
    except OSError:
        pass  # no history file yet — first run
    readline.set_history_length(1000)

    def _save():
        try:
            readline.write_history_file(CMD_HISTORY_PATH)
        except OSError:
            pass

    atexit.register(_save)


def read_history(hours: float) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=hours)
    rows = []
    with HISTORY_PATH.open(newline="") as f:
        for r in csv.DictReader(f):
            try:
                ts = datetime.fromisoformat(r["timestamp"])
            except (ValueError, KeyError):
                continue
            if ts >= cutoff:
                r["_ts"] = ts
                rows.append(r)
    return rows


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def fmt_temp(v) -> str:
    try:
        return f"{float(v):.1f}°C"
    except (TypeError, ValueError):
        return "—"


def enum_name(v) -> str:
    """Enums expose .name; some fields come back as raw ints (e.g. custom fan)."""
    if v is None:
        return "—"
    return getattr(v, "name", str(v))


def parse_when(spec: str) -> tuple[float, str]:
    """Parse '30m' / '1h30m' / '90s' / 'HH:MM' -> (seconds_from_now, human_desc).

    Raises ValueError on anything it can't make sense of.
    """
    spec = spec.strip()
    if ":" in spec:  # absolute clock time today (or tomorrow if already past)
        hh, mm = spec.split(":")
        now = datetime.now()
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds(), target.strftime("%H:%M")
    total = 0
    for num, unit in re.findall(r"(\d+)\s*([smh])", spec.lower()):
        total += int(num) * {"s": 1, "m": 60, "h": 3600}[unit]
    if total <= 0:
        raise ValueError(f"could not parse duration {spec!r}")
    fire = datetime.now() + timedelta(seconds=total)
    return total, f"in {spec} (at {fire.strftime('%H:%M')})"


def show_status(device: AC) -> None:
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column(style="bold")
    power = "[green]ON[/green]" if device.power_state else "[red]OFF[/red]"
    t.add_row("Power", power)
    t.add_row("Mode", enum_name(device.operational_mode))
    t.add_row("Fan", enum_name(device.fan_speed))
    t.add_row("Target temp", fmt_temp(device.target_temperature))
    t.add_row("Home temp", fmt_temp(device.indoor_temperature))
    t.add_row("Outdoor temp", fmt_temp(device.outdoor_temperature))
    if device.indoor_humidity is not None:
        t.add_row("Home humidity", f"{device.indoor_humidity}%")
    t.add_row("Online", "yes" if device.online else "[red]no[/red]")
    console.print(t)


def show_chart(hours: float) -> None:
    rows = read_history(hours)
    if len(rows) < 2:
        console.print(
            "[yellow]Not enough history yet.[/yellow] The poller records a sample "
            "on each interval — leave the app running a while, then try `chart`."
        )
        return

    # We plot against epoch seconds (a plain numeric x-axis) and format the tick
    # labels ourselves. plotext's own H:M date_form mishandles the string->number
    # round-trip and shifts every label by a phantom offset, so the axis ends up
    # showing times the data never covered (e.g. labels running into the future).
    series = {"outdoor": ("outdoor", []), "indoor": ("home", []), "target": ("target", [])}
    times = {k: [] for k in series}
    for r in rows:
        for key in series:
            try:
                val = float(r[key])
            except (TypeError, ValueError):
                continue
            series[key][1].append(val)
            times[key].append(r["_ts"].timestamp())

    all_x = [x for xs in times.values() for x in xs]
    if not all_x:
        console.print("[yellow]No numeric temperature data to plot yet.[/yellow]")
        return
    x_min, x_max = min(all_x), max(all_x)

    # AC on/off state. The poller (and smart mode) records power=1/0 on every
    # sample, so we can show when — and for how long — the unit ran. Drawn as a
    # step line in a band just below the temperatures so its width reads as
    # duration without colliding with the °C lines.
    p_times, p_vals = [], []
    for r in rows:
        try:
            p_vals.append(1.0 if float(r["power"]) >= 0.5 else 0.0)
        except (TypeError, ValueError, KeyError):
            continue
        p_times.append(r["_ts"].timestamp())

    on_secs = off_secs = 0.0
    for i in range(len(p_times) - 1):
        dt = p_times[i + 1] - p_times[i]
        if p_vals[i] >= 0.5:
            on_secs += dt
        else:
            off_secs += dt

    plt.clf()
    plt.title(f"Temperatures — last {hours:g}h")
    plt.xlabel("time")
    plt.ylabel("°C")
    for key, (label, ys) in series.items():
        if ys:
            plt.plot(times[key], ys, label=label, marker="braille")

    if len(p_times) >= 2:
        temps = [v for _, ys in series.values() for v in ys]
        t_lo = min(temps)
        gap = max(0.5, (max(temps) - t_lo) * 0.12)
        off_y, on_y = t_lo - gap * 1.6, t_lo - gap * 0.6
        # Build a square step series so on/off periods read as flat bands of
        # the right width rather than diagonal ramps between samples.
        step_x, step_y = [], []
        prev = None
        for t, v in zip(p_times, p_vals):
            y = on_y if v >= 0.5 else off_y
            if prev is not None:
                step_x.append(t)
                step_y.append(prev)
            step_x.append(t)
            step_y.append(y)
            prev = y
        plt.plot(step_x, step_y, label="AC on/off", marker="braille")

    # Evenly spaced ticks across the actual data range, labelled from real times.
    span = x_max - x_min
    tick_fmt = "%m-%d %H:%M" if span > 24 * 3600 else "%H:%M"
    n_ticks = 5
    if span <= 0:
        positions = [x_min]
    else:
        positions = [x_min + span * i / (n_ticks - 1) for i in range(n_ticks)]
    labels = [datetime.fromtimestamp(p).strftime(tick_fmt) for p in positions]
    plt.xticks(positions, labels)

    plt.plotsize(80, 22)
    plt.theme("pro")
    plt.show()

    total = on_secs + off_secs
    if total > 0:
        def _dur(s: float) -> str:
            h, m = divmod(int(round(s / 60)), 60)
            return f"{h}h {m:02d}m" if h else f"{m}m"

        console.print(
            f"AC [green]on[/green] {_dur(on_secs)} · [red]off[/red] {_dur(off_secs)} "
            f"· duty {on_secs / total * 100:.0f}%"
        )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
HELP = """\
[bold]Commands[/bold]
  on / off            power the unit on or off
  temp <n>            set target temperature (e.g. temp 23)
  mode <m>            cool | heat | auto | dry | fan
  fan <f>             auto | low | medium | high | max | silent
  status              show current state
  chart [hours]       plot outdoor / home / target temp (default 6)
  timer <when>        turn off after 30m / 1h30m / HH:MM
  timer cool 23 30m   set+on now, then off after that time
  timer cancel        cancel a pending timer
  smart <temp> [band] thermostat: on above temp+band, off at temp
                      (echoes temps each cycle so you needn't run status)
  smart off           disable smart mode
  poll <seconds>      change background sampling interval
  help                show this help
  quit / exit         leave the app

[dim]Tip: any of the above also runs directly from your shell, e.g.
`midea status` or `midea mode cool` — runs once and exits.[/dim]\
"""

MODE_MAP = {
    "cool": AC.OperationalMode.COOL,
    "heat": AC.OperationalMode.HEAT,
    "auto": AC.OperationalMode.AUTO,
    "dry": AC.OperationalMode.DRY,
    "fan": AC.OperationalMode.FAN_ONLY,
}
FAN_MAP = {
    "auto": AC.FanSpeed.AUTO,
    "low": AC.FanSpeed.LOW,
    "medium": AC.FanSpeed.MEDIUM,
    "med": AC.FanSpeed.MEDIUM,
    "high": AC.FanSpeed.HIGH,
    "max": AC.FanSpeed.MAX,
    "silent": AC.FanSpeed.SILENT,
}


class Controller:
    def __init__(self, device: AC, cfg: dict):
        self.device = device
        self.cfg = cfg
        self.io_lock = asyncio.Lock()  # serialise all device I/O
        self.poll_interval = cfg.get("poll_interval", 60)
        self._stop = asyncio.Event()
        self.timer_task: asyncio.Task | None = None
        self.timer_fire_at: datetime | None = None
        self.timer_desc: str = ""
        # smart (software thermostat) loop
        self.smart_enabled = False
        self.smart_target: float | None = None
        self.smart_deadband = 0.5
        self.smart_interval = 30  # seconds between checks
        self.smart_min_cycle = 180  # min seconds between on/off switches (compressor safety)
        self.smart_task: asyncio.Task | None = None
        self.smart_last_switch: datetime | None = None

    async def poller(self):
        """Background loop: refresh and record a sample every interval."""
        while not self._stop.is_set():
            try:
                async with self.io_lock:
                    await self.device.refresh()
                record_sample(self.device)
            except Exception as e:  # keep the loop alive on transient errors
                console.print(f"[dim red]poll error: {e}[/dim red]")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def apply(self):
        async with self.io_lock:
            await self.device.apply()

    # --- auto-off timer ------------------------------------------------------
    def cancel_timer(self):
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
        self.timer_task = None
        self.timer_fire_at = None
        self.timer_desc = ""

    def set_off_timer(self, delay: float, desc: str):
        self.cancel_timer()
        self.timer_fire_at = datetime.now() + timedelta(seconds=delay)
        self.timer_desc = desc
        self.timer_task = asyncio.create_task(self._timer_runner(delay, desc))

    async def _timer_runner(self, delay: float, desc: str):
        try:
            await asyncio.sleep(delay)
            async with self.io_lock:
                self.device.power_state = False
                await self.device.apply()
            record_sample(self.device)
            console.print(f"\n[bold]Timer fired[/bold] — turned off ({desc}).\nmidea> ", end="")
        except asyncio.CancelledError:
            pass
        finally:
            self.timer_fire_at = None
            self.timer_desc = ""

    # --- smart thermostat (software bang-bang control) -----------------------
    def start_smart(self, target: float, deadband: float):
        self.stop_smart()
        self.smart_target = target
        self.smart_deadband = deadband
        self.smart_enabled = True
        self.smart_last_switch = None
        self.smart_task = asyncio.create_task(self._smart_runner())

    def stop_smart(self):
        self.smart_enabled = False
        if self.smart_task and not self.smart_task.done():
            self.smart_task.cancel()
        self.smart_task = None

    async def _smart_runner(self):
        try:
            while self.smart_enabled and not self._stop.is_set():
                try:
                    async with self.io_lock:
                        await self.device.refresh()
                    temp = self.device.indoor_temperature
                    if temp is not None:
                        await self._smart_step(float(temp))
                    record_sample(self.device)
                    self._echo_smart_temps()
                except Exception as e:
                    console.print(f"[dim red]smart error: {e}[/dim red]")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.smart_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _echo_smart_temps(self):
        """Refresh the smart readings on the blank line just above the prompt.

        While smart mode is on the interactive prompt carries a leading newline
        (see main()), so there's always an empty line directly above "midea> ".
        We save the cursor, hop up onto that line, repaint the readings there, and
        restore the cursor. The prompt line itself — and anything the user is
        half-typing on it — is never touched, and readline never sees the cursor
        move, so nothing stacks and no input is disturbed.

        We deliberately do NOT consult readline.get_line_buffer(): on macOS
        (libedit) a cross-thread read returns the previous *submitted* line rather
        than the live buffer, which once made the echo skip forever. We don't need
        it here anyway, since we never redraw the prompt line.
        """
        d = self.device
        ts = datetime.now().strftime("%H:%M:%S")
        state = "\033[32mON \033[0m" if d.power_state else "\033[90mOFF\033[0m"
        status = (
            f"\033[2m{ts}\033[0m {state} "
            f"home {fmt_temp(d.indoor_temperature)} · "
            f"target {self.smart_target:g}°C · "
            f"out {fmt_temp(d.outdoor_temperature)}"
        )
        # Without a real terminal we can't position the cursor; just print a line.
        if not sys.stdout.isatty():
            console.print(status)
            return

        out = sys.stdout
        # \0337 save cursor · \033[1A up to the reserved blank line · \r\033[2K wipe
        # it · write the readings · \0338 restore cursor back onto the prompt line.
        out.write(f"\0337\033[1A\r\033[2K  {status}\0338")
        out.flush()

    async def _smart_step(self, temp: float):
        """One control decision: on above target+deadband, off at/below target."""
        on = bool(self.device.power_state)
        on_thresh = self.smart_target + self.smart_deadband
        off_thresh = self.smart_target

        # Respect the minimum time between switches (compressor protection).
        if self.smart_last_switch is not None:
            elapsed = (datetime.now() - self.smart_last_switch).total_seconds()
            if elapsed < self.smart_min_cycle:
                return

        if not on and temp >= on_thresh:
            async with self.io_lock:
                self.device.power_state = True
                self.device.operational_mode = AC.OperationalMode.COOL
                self.device.target_temperature = self.smart_target
                await self.device.apply()
            self.smart_last_switch = datetime.now()
            console.print(
                f"\n[green]smart: {temp:.1f}°C ≥ {on_thresh:.1f} → ON "
                f"(cool {self.smart_target:g})[/green]\nmidea> ",
                end="",
            )
        elif on and temp <= off_thresh:
            async with self.io_lock:
                self.device.power_state = False
                await self.device.apply()
            self.smart_last_switch = datetime.now()
            console.print(
                f"\n[cyan]smart: {temp:.1f}°C ≤ {off_thresh:.1f} → OFF[/cyan]"
                f"\nmidea> ",
                end="",
            )

    async def _handle_smart(self, args: list[str]):
        usage = (
            "usage: smart <temp> [deadband]   hold home at/under temp by cycling on/off\n"
            "       smart off                  disable smart mode"
        )
        if not args:
            if self.smart_enabled:
                console.print(
                    f"Smart: ON, target {self.smart_target:g}°C "
                    f"(on ≥{self.smart_target + self.smart_deadband:g}, off ≤{self.smart_target:g}; "
                    f"check {self.smart_interval}s, ≥{self.smart_min_cycle}s between switches)."
                )
            else:
                console.print("Smart: off.\n" + usage)
            return
        if args[0].lower() == "off":
            if self.smart_enabled:
                self.stop_smart()
                console.print("Smart mode disabled.")
            else:
                console.print("Smart mode is not on.")
            return
        try:
            target = float(args[0])
        except ValueError:
            console.print("[red]smart: target must be a number[/red]\n" + usage)
            return
        deadband = 0.5
        if len(args) >= 2:
            try:
                deadband = float(args[1])
            except ValueError:
                console.print("[red]smart: deadband must be a number[/red]")
                return
        self.start_smart(target, deadband)
        console.print(
            f"Smart mode ON — holding home ≤ {target:g}°C "
            f"(on at ≥{target + deadband:g}, off at ≤{target:g}). Checking and "
            f"echoing temps every {self.smart_interval}s."
        )

    async def handle(self, line: str) -> bool:
        """Process one command. Return False to exit."""
        parts = line.split()
        if not parts:
            return True
        cmd, *args = parts
        cmd = cmd.lower()

        if cmd in ("quit", "exit", "q"):
            return False
        elif cmd in ("help", "h", "?"):
            console.print(HELP)
        elif cmd == "on":
            self.device.power_state = True
            await self.apply()
            console.print("[green]Turned on[/green]")
        elif cmd == "off":
            self.device.power_state = False
            await self.apply()
            console.print("[red]Turned off[/red]")
        elif cmd == "temp":
            if not args:
                console.print("usage: temp <number>")
            else:
                try:
                    self.device.target_temperature = float(args[0])
                    await self.apply()
                    console.print(f"Target set to {fmt_temp(self.device.target_temperature)}")
                except ValueError:
                    console.print("[red]temp needs a number[/red]")
        elif cmd == "mode":
            m = MODE_MAP.get(args[0].lower()) if args else None
            if m is None:
                console.print(f"usage: mode <{'|'.join(MODE_MAP)}>")
            else:
                self.device.operational_mode = m
                await self.apply()
                console.print(f"Mode set to {m.name}")
        elif cmd == "fan":
            f = FAN_MAP.get(args[0].lower()) if args else None
            if f is None:
                console.print(f"usage: fan <{'|'.join(sorted(set(FAN_MAP)))}>")
            else:
                self.device.fan_speed = f
                await self.apply()
                console.print(f"Fan set to {f.name}")
        elif cmd == "status":
            async with self.io_lock:
                await self.device.refresh()
            show_status(self.device)
            if self.smart_enabled:
                console.print(
                    f"[dim]smart: ON, target {self.smart_target:g}°C[/dim]"
                )
            if self.timer_fire_at:
                console.print(
                    f"[dim]timer: off at {self.timer_fire_at.strftime('%H:%M')}[/dim]"
                )
        elif cmd == "chart":
            hours = 6.0
            if args:
                try:
                    hours = float(args[0])
                except ValueError:
                    console.print("[red]chart needs a number of hours[/red]")
                    return True
            show_chart(hours)
        elif cmd == "poll":
            if args:
                try:
                    self.poll_interval = max(5, int(args[0]))
                    console.print(f"Poll interval set to {self.poll_interval}s")
                except ValueError:
                    console.print("[red]poll needs a number of seconds[/red]")
            else:
                console.print(f"Poll interval is {self.poll_interval}s")
        elif cmd == "timer":
            await self._handle_timer(args)
        elif cmd == "smart":
            await self._handle_smart(args)
        else:
            console.print(f"Unknown command: {cmd}. Type 'help'.")
        return True

    async def _handle_timer(self, args: list[str]):
        usage = (
            "usage: timer <30m|1h30m|HH:MM>            turn off after that time\n"
            "       timer <mode> <temp> <when>         set+on now, off after that time\n"
            "       timer cancel                        cancel a pending timer"
        )
        if not args:
            if self.timer_fire_at:
                console.print(
                    f"Timer set: turns off {self.timer_desc} "
                    f"(at {self.timer_fire_at.strftime('%H:%M:%S')})."
                )
            else:
                console.print("No timer set.\n" + usage)
            return
        if args[0].lower() == "cancel":
            if self.timer_fire_at:
                self.cancel_timer()
                console.print("Timer cancelled.")
            else:
                console.print("No timer to cancel.")
            return

        # Optional 'mode temp' prefix means: apply those + power on now.
        when_spec = args[-1]
        if len(args) >= 3:
            mode = MODE_MAP.get(args[0].lower())
            if mode is None:
                console.print(usage)
                return
            try:
                temp = float(args[1])
            except ValueError:
                console.print("[red]timer: temperature must be a number[/red]")
                return
            self.device.power_state = True
            self.device.operational_mode = mode
            self.device.target_temperature = temp
            await self.apply()
            console.print(f"On, {mode.name} {fmt_temp(temp)}.")
        elif len(args) != 1:
            console.print(usage)
            return

        try:
            delay, desc = parse_when(when_spec)
        except ValueError as e:
            console.print(f"[red]timer: {e}[/red]\n" + usage)
            return
        self.set_off_timer(delay, desc)
        console.print(f"Will turn off {desc}.")

    def stop(self):
        self._stop.set()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def main(command: str | None = None):
    cfg = load_config()
    if cfg is None:
        cfg = await setup_device()

    console.print(f"[cyan]Connecting to {cfg.get('name','AC')} at {cfg['ip']}…[/cyan]")
    try:
        device = await connect(cfg)
    except Exception as e:
        console.print(f"[red]Could not connect: {e}[/red]")
        console.print("If the IP changed, delete config.json and re-run to rediscover.")
        sys.exit(1)

    ctrl = Controller(device, cfg)
    record_sample(device)

    console.print(f"[green]Connected.[/green]")
    if command is None:
        show_status(device)

    # Single-shot mode: `midea <command...>` (e.g. `midea on`, `midea mode
    # cool`, `midea status`) runs exactly one command through the same
    # handler the interactive shell uses, then exits — no REPL, no TTY
    # required. Every command in `help` works here too, except that `timer`,
    # `smart`, and `poll <n>` only report their result: their effect relies
    # on a background task that can't outlive this process once it exits.
    if command is not None:
        try:
            await ctrl.handle(command)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            sys.exit(1)
        ctrl.cancel_timer()
        ctrl.stop_smart()
        return

    # Headless (no TTY, no explicit command): show status, record a sample,
    # and exit cleanly.
    if not sys.stdin.isatty():
        console.print(
            "\n[yellow]No interactive terminal detected[/yellow] — showed status and "
            "recorded a sample.\nRun this in a real terminal (Terminal.app / iTerm) "
            "for the command prompt, or pass a command directly, e.g. `midea status`."
        )
        return

    console.print("Type 'help' for commands.")
    init_command_history()
    poll_task = asyncio.create_task(ctrl.poller())
    try:
        while True:
            try:
                # Read input in a thread so the background poller keeps running.
                # The leading newline leaves a blank line above the prompt; while
                # smart mode is on, the live readings are painted onto that line
                # (see _echo_smart_temps) so the prompt itself stays clean.
                line = await asyncio.to_thread(input, "\nmidea> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not await ctrl.handle(line):
                break
    finally:
        ctrl.stop()
        ctrl.cancel_timer()
        ctrl.stop_smart()
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        console.print("Bye.")


def cli() -> None:
    """Console-script entry point (`midea`), wired up in pyproject.toml.

    No arguments  -> interactive shell (unchanged).
    Arguments     -> run one command non-interactively and exit, e.g.:
                       midea on
                       midea mode cool
                       midea status
                     Supports every command `help` lists inside the shell.
    """
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        console.print(f"Usage: midea [COMMAND [ARGS...]]\n\n{HELP}")
        return
    command = " ".join(args) if args else None
    try:
        asyncio.run(main(command))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
