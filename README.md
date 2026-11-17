# Vigilante de luz (WeCo / NOOR)

Avisa al móvil (vía [ntfy](https://ntfy.sh)) cuando se va o vuelve la corriente de la calle,
usando los datos que un inversor WeCo envía a la nube de la app NOOR.

- Señal: voltaje de red (fases A/B) de la gráfica de NOOR; < 50 V = sin luz.
- Avisos: se fue la luz · volvió la luz (con duración) · batería baja durante el apagón ·
  inversor sin conexión · token caducado.
- Corre en GitHub Actions cada ~5 min; el estado entre ejecuciones se guarda en la caché de Actions.

## Configuración
Secrets del repo: `NOOR_TOKEN`, `NOOR_SN`, `NTFY_TOPIC`. En el iPhone: app **ntfy** → suscribirse al mismo tema.

Prueba: Actions → *Vigilante de luz* → *Run workflow* con "test" marcado.

Local: `python3 monitor.py --dry-run` (lee `.env`).

> GitHub desactiva los workflows programados tras 60 días sin actividad en el repo (avisa por email);
> basta con reactivarlo desde la pestaña Actions.
