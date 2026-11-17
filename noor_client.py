#!/usr/bin/env python3
"""Cliente mínimo (solo lectura) de la API de NOOR / WeCo. Ver NOOR_API.md.

Uso:
    python3 noor_client.py            # estado en tiempo real
    python3 noor_client.py --watch 300  # repetir cada 300 s
    python3 noor_client.py --json     # salida JSON cruda
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

BASE_URL = "https://noor.olpess-api.com"
APP_VERSION = "1.2.8"


class NoorError(Exception):
    pass


class TokenExpired(NoorError):
    pass


def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Realtime:
    online: bool
    last_update: str
    system_status: str
    work_mode: str
    pv_kw: float
    load_kw: float
    grid_kw: float
    battery_kw: float
    battery_soc: int
    battery_voltage: float

    @classmethod
    def from_api(cls, d: dict) -> "Realtime":
        return cls(
            online=bool(d.get("online")),
            last_update=d.get("lastUpdateTime") or "",
            system_status=d.get("systemStatus") or "",
            work_mode=d.get("workMode") or "",
            pv_kw=float(d.get("pvPower") or 0),
            load_kw=float(d.get("loadPower") or 0),
            grid_kw=float(d.get("gridPower") or 0),
            battery_kw=float(d.get("batPower") or 0),
            battery_soc=int(d.get("batSoc") or 0),
            battery_voltage=float(d.get("batVoltage") or 0),
        )


class NoorClient:
    def __init__(self, token: str, base_url: str = BASE_URL, timeout: float = 15):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": self.token,
                "Appversion": APP_VERSION,
                "Logintool": "Phone",
                "Accept": "application/json",
                "Accept-Language": "en",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode())
        except urllib.error.URLError as e:
            raise NoorError(f"Error de red: {e}") from e
        if not payload.get("success"):
            msg = payload.get("errMessage") or "error desconocido"
            if "token" in msg.lower():
                raise TokenExpired(msg)
            raise NoorError(msg)
        return payload.get("data")

    # --- planta ---
    def plants(self) -> list[dict]:
        return self._request("POST", "/api/plant/homePlantPage",
                             {"keyWord": "", "pageIndex": 1, "pageSize": 50}) or []

    def plant_details(self, plant_id: int) -> dict:
        return self._request("GET", f"/api/plant/getPlantDetails?id={plant_id}")

    def device_list(self, plant_id: int) -> dict:
        return self._request("GET", f"/api/plant/getDeviceList/{plant_id}")

    def error_count(self, plant_id: int) -> int:
        return int(self._request("GET", f"/api/home/getPlantErrorCount?plantId={plant_id}") or 0)

    # --- inversor ---
    def realtime_raw(self, sn: str) -> dict:
        return self._request("GET", f"/api/home/getSenergyHome/{sn}")

    def realtime(self, sn: str) -> Realtime:
        return Realtime.from_api(self.realtime_raw(sn))

    def inverter_info(self, device_id: int,
                      groups: str = "PV Info,Inverter Info(Home Model Info)") -> dict[str, dict]:
        q = urllib.parse.quote(groups)
        raw = self._request("GET", f"/inverter/{device_id}/info/groupsV2?groupNames={q}") or []
        return {g["groupName"]: {f["fieldName"]: (f["fieldValue"], f.get("unit"))
                                 for f in g.get("fields", [])} for g in raw}

    def energy(self, sn: str, date: dt.date | None = None, query_type: int = 0) -> dict:
        date = date or dt.date.today()
        return self._request("POST", "/api/home/getInverterHomeEnergyV2",
                             {"serialNumberList": [sn], "queryType": query_type,
                              "date": date.isoformat()})

    def chart_fields(self, sn: str, chart_type: int = 1001) -> list[dict]:
        return self._request("GET", f"/data/chart/queryField?deviceSn={sn}&type={chart_type}")

    def chart(self, sn: str, field_ids: list[int], date: dt.date | None = None,
              chart_type: int = 1001) -> list[dict]:
        date = date or dt.date.today()
        return self._request("POST", "/data/chart/getChartData", {
            "type": chart_type,
            "start": f"{date} 00:00:00",
            "end": f"{date} 23:59:59",
            "queryItems": [{"ids": field_ids, "deviceSn": sn}],
        })


def print_status(c: NoorClient, sn: str) -> None:
    rt = c.realtime(sn)
    flow = "cargando" if rt.battery_kw > 0 else "descargando" if rt.battery_kw < 0 else "en reposo"
    print(f"[{rt.last_update}] {'🟢 online' if rt.online else '🔴 offline'} · {rt.system_status} · {rt.work_mode}")
    print(f"  ☀️  Solar    {rt.pv_kw:6.2f} kW")
    print(f"  🏠 Consumo  {rt.load_kw:6.2f} kW")
    print(f"  🔌 Red      {rt.grid_kw:6.2f} kW")
    print(f"  🔋 Batería  {rt.battery_kw:6.2f} kW ({flow}) · {rt.battery_soc}% · {rt.battery_voltage} V")


def main() -> int:
    load_env()
    p = argparse.ArgumentParser(description="Estado del inversor WeCo vía API de NOOR")
    p.add_argument("--sn", default=os.environ.get("NOOR_SN"))
    p.add_argument("--json", action="store_true", help="imprimir JSON")
    p.add_argument("--watch", type=int, metavar="SEG", help="repetir cada N segundos")
    a = p.parse_args()

    token = os.environ.get("NOOR_TOKEN")
    if not token or not a.sn:
        print("Faltan NOOR_TOKEN y/o NOOR_SN (en .env o en el entorno).", file=sys.stderr)
        return 1
    c = NoorClient(token)
    while True:
        try:
            if a.json:
                print(json.dumps(asdict(c.realtime(a.sn)), ensure_ascii=False))
            else:
                print_status(c, a.sn)
        except TokenExpired:
            print("El token caducó: hay que capturar uno nuevo desde la app NOOR.", file=sys.stderr)
            return 2
        except NoorError as e:
            print(f"Error: {e}", file=sys.stderr)
            if not a.watch:
                return 1
        if not a.watch:
            return 0
        time.sleep(a.watch)


if __name__ == "__main__":
    sys.exit(main())
