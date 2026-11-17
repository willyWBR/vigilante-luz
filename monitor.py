#!/usr/bin/env python3
"""Vigilante de apagones: consulta la API de NOOR y avisa por ntfy cuando
se va o vuelve la luz de la calle (red eléctrica).

Variables de entorno (o .env):
    NOOR_TOKEN   token de la app NOOR (cabecera Authorization)
    NOOR_SN      número de serie del inversor
    NTFY_TOPIC   tema de ntfy al que estás suscrito en el iPhone
    NTFY_SERVER  opcional, por defecto https://ntfy.sh
    LOW_SOC      opcional, % de batería para avisar durante un apagón (defecto 30)
    TZ_NAME      opcional, zona horaria de la planta (defecto America/Santo_Domingo)
    STATE_FILE   opcional, defecto state.json

Uso:
    python3 monitor.py           # una comprobación (lo que hace GitHub Actions)
    python3 monitor.py --test    # manda una notificación de prueba
    python3 monitor.py --dry-run # no manda nada, solo imprime
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from noor_client import NoorClient, NoorError, TokenExpired, load_env

GRID_VOLTAGE_IDS = [11480, 11481]  # aPhaseGridVoltage, bPhaseGridVoltage
GRID_ON_VOLTS = 50.0
STALE_MINUTES = 30


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        sys.exit(f"Falta la variable {name}")
    return v


# ---------- notificaciones ----------

def notify(title: str, message: str, *, priority: int = 3, tags: list[str] | None = None,
           dry_run: bool = False) -> None:
    print(f"🔔 {title} — {message}")
    if dry_run:
        return
    body = {"topic": env("NTFY_TOPIC"), "title": title, "message": message,
            "priority": priority, "tags": tags or []}
    req = urllib.request.Request(
        os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


# ---------- estado entre ejecuciones ----------

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=1, ensure_ascii=False))


# ---------- lectura de la red ----------

def grid_series(c: NoorClient, sn: str, today: dt.date) -> list[dict]:
    """Puntos (cada 5 min) con voltaje de red, de ayer y hoy, sin los vacíos."""
    pts: list[dict] = []
    for day in (today - dt.timedelta(days=1), today):
        for p in c.chart(sn, GRID_VOLTAGE_IDS, day) or []:
            if p.get("aPhaseGridVoltage") is not None or p.get("bPhaseGridVoltage") is not None:
                pts.append(p)
    return pts


def is_on(p: dict) -> bool:
    return max(float(p.get("aPhaseGridVoltage") or 0), float(p.get("bPhaseGridVoltage") or 0)) > GRID_ON_VOLTS


def transition_time(pts: list[dict], on: bool) -> str:
    """Hora del primer punto del estado actual tras el último cambio."""
    t = pts[-1]["time"]
    for p in reversed(pts):
        if is_on(p) != on:
            break
        t = p["time"]
    return t


def fmt_duration(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h} h {m} min" if h else f"{m} min"


def parse_local(s: str, tz: ZoneInfo) -> dt.datetime:
    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=tz)


# ---------- principal ----------

def check(dry_run: bool) -> int:
    tz = ZoneInfo(os.environ.get("TZ_NAME", "America/Santo_Domingo"))
    now = dt.datetime.now(tz)
    state_path = Path(os.environ.get("STATE_FILE", "state.json"))
    state = load_state(state_path)
    low_soc = int(os.environ.get("LOW_SOC", "30"))
    sn = env("NOOR_SN")
    c = NoorClient(env("NOOR_TOKEN"))

    try:
        rt = c.realtime_raw(sn)
        pts = grid_series(c, sn, now.date())
    except TokenExpired:
        if not state.get("token_alert"):
            notify("⚠️ Vigilante de luz detenido",
                   "El token de NOOR caducó. Hay que capturar uno nuevo y actualizar el secret NOOR_TOKEN.",
                   priority=4, tags=["warning"], dry_run=dry_run)
            state["token_alert"] = True
            save_state(state_path, state)
        return 0
    except NoorError as e:
        print(f"API no disponible: {e} (se reintentará en la próxima ejecución)")
        return 0
    state["token_alert"] = False

    # ¿El inversor está reportando?
    last_update = parse_local(rt.get("lastUpdateTime") or "1970-01-01 00:00:00", tz)
    stale_min = (now - last_update).total_seconds() / 60
    if not rt.get("online") or stale_min > STALE_MINUTES:
        if not state.get("stale_alert"):
            notify("📡 Inversor sin conexión",
                   f"NOOR no recibe datos desde {last_update:%H:%M} ({fmt_duration(stale_min)}). "
                   "Puede ser el internet de casa o un apagón largo.",
                   priority=3, tags=["satellite"], dry_run=dry_run)
            state["stale_alert"] = True
        save_state(state_path, state)
        print(f"Datos viejos ({fmt_duration(stale_min)}); no cambio el estado de la red.")
        return 0
    if state.get("stale_alert"):
        notify("📡 Inversor conectado de nuevo", "NOOR vuelve a recibir datos.",
               priority=2, tags=["satellite"], dry_run=dry_run)
        state["stale_alert"] = False

    if not pts:
        print("Sin puntos de voltaje de red todavía.")
        save_state(state_path, state)
        return 0

    on = is_on(pts[-1])
    soc = rt.get("batSoc")
    prev = state.get("grid")
    since = transition_time(pts, on)
    print(f"{now:%Y-%m-%d %H:%M} red={'ON' if on else 'OFF'} (desde {since}) "
          f"V={pts[-1].get('aPhaseGridVoltage')}/{pts[-1].get('bPhaseGridVoltage')} "
          f"batería={soc}% estado_anterior={prev}")

    if prev is None:
        print("Primera ejecución: guardo el estado sin avisar.")
    elif prev == "on" and not on:
        notify("⚡ Se fue la luz",
               f"Sin corriente de la calle desde las {since[-5:]}. Batería al {soc}%.",
               priority=5, tags=["warning", "electric_plug"], dry_run=dry_run)
    elif prev == "off" and on:
        start = state.get("since")
        dur = ""
        if start:
            dur = f" Duró {fmt_duration((parse_local(since, tz) - parse_local(start, tz)).total_seconds() / 60)}."
        notify("💡 Volvió la luz",
               f"Corriente de la calle desde las {since[-5:]}.{dur} Batería al {soc}%.",
               priority=4, tags=["bulb"], dry_run=dry_run)

    if not on and soc is not None and int(soc) <= low_soc and not state.get("low_soc_alert"):
        notify("🔋 Batería baja durante el apagón",
               f"La batería está al {soc}% y sigue sin luz de la calle.",
               priority=5, tags=["battery"], dry_run=dry_run)
        state["low_soc_alert"] = True
    if on:
        state["low_soc_alert"] = False

    if prev != ("on" if on else "off"):
        state["since"] = since
    state["grid"] = "on" if on else "off"
    state["checked"] = now.isoformat(timespec="minutes")
    save_state(state_path, state)
    return 0


def main() -> int:
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--test", action="store_true", help="enviar notificación de prueba")
    p.add_argument("--dry-run", action="store_true", help="no enviar notificaciones")
    a = p.parse_args()
    if a.test:
        notify("✅ Prueba del vigilante de luz", "Si ves esto, las notificaciones funcionan.",
               tags=["white_check_mark"], dry_run=a.dry_run)
        return 0
    return check(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
