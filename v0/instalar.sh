#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Gabriel González Pineda <https://github.com/phyrexia>
# SPDX-License-Identifier: Apache-2.0
# Instala, verifica o revierte wtr-ventiladores (control de SYS y SATA) y wtr-guardia-discos (guardia
# térmica de los discos) en el WTR MAX. Ejecutar como root en el host, con los archivos de este
# directorio junto al script. Documentación: README del repositorio wtr-max-fans
# (https://github.com/phyrexia/wtr-max-fans).
#
#   bash instalar.sh guardia            instala o actualiza sólo la guardia y la arranca (paso 2)
#   bash instalar.sh probar-sandbox     smartctl y shutdown -k +1 bajo el sandbox de la guardia (H2)
#   bash instalar.sh simular            guardia + controlador con --simular: no escribe en sysfs (paso 3)
#   bash instalar.sh instalar           guardia + controlador real (paso 4)
#   bash instalar.sh verificar          sólo comprueba (sale 1 si algo no cuadra)
#   bash instalar.sh revertir [--bios] [--tambien-guardia]
#       retira el controlador (ExecStopPost deja SATA 153 / SYS 77); --bios devuelve Start temp 20 °C
#       y Start PWM 100; la guardia sólo se retira con --tambien-guardia.
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly AQUI
readonly SBIN=/usr/local/sbin
readonly DATOS=/usr/local/share/wtr-ventiladores
readonly UNIDADES=/etc/systemd/system
readonly TEXTFILE=/var/lib/prometheus/node-exporter
readonly DROPIN="$UNIDADES/wtr-ventiladores.service.d/simular.conf"
readonly VERSION_DKMS=v2.0-6-gbd7a566.20261002.wtr1
# sha256 de las versiones de cada .conf distribuidas antes (separadas por espacios). Un /etc/<nombre> con uno de
# estos hashes no se editó en el host y `copiar_conf` lo reemplaza por el del repo (enmienda rev. 5.1, §6). Nunca
# el hash de la versión actual del repo.
declare -rA CONF_PREVIAS=(
  [wtr-ventiladores.conf]=27dfc55c2479adf916aee5a1eff817a307500a2716f9a55445ca2fdbe5da5cc3   # bbf1050 (rev. 4)
  [wtr-guardia-discos.conf]=''                                                                 # sin versiones previas
)

morir() { printf 'instalar.sh: %s\n' "$*" >&2; exit 1; }
info() { printf 'instalar.sh: %s\n' "$*"; }

(( EUID == 0 )) || morir 'debe ejecutarse como root'
[[ "$(hostname -s)" == wtrmax ]] || morir "sólo para el WTR MAX, no para $(hostname -s)"

hwmon() {
  local h
  for h in /sys/devices/platform/it87.2608/hwmon/hwmon*; do
    [[ -r "$h/name" && "$(cat "$h/name")" == it8613 ]] && { printf '%s\n' "$h"; return 0; }
  done
  return 1
}

claves_conf() {   # $1 = archivo .conf; imprime sus claves, ordenadas
  sed -nE 's/^[[:space:]]*([A-Z_][A-Z0-9_]*)[[:space:]]*=.*/\1/p' "$1" | LC_ALL=C sort -u
}

copiar_conf() {   # $1 = nombre en este directorio; destino /etc/$1. Nunca pisa una edición del host.
  local destino="/etc/$1" vivo anterior faltan
  if [[ ! -e "$destino" ]]; then
    install -o root -g root -m 0644 "$AQUI/$1" "$destino"
    return 0
  fi
  if cmp -s "$destino" "$AQUI/$1"; then
    info "$destino ya es la versión del repo"
    return 0
  fi
  vivo="$(sha256sum "$destino" | cut -d' ' -f1)"
  if [[ " ${CONF_PREVIAS[$1]:-} " == *" $vivo "* ]]; then
    # Versión distribuida antes, sin editar: copia primero y reemplazo atómico en el mismo sistema de
    # archivos. El servicio relee el archivo en cada ciclo y nunca lo ve a medias ni ausente.
    anterior="$destino.anterior-$(date +%Y%m%dT%H%M%S)"
    cp -p "$destino" "$anterior"
    install -o root -g root -m 0644 "$AQUI/$1" "$destino.nuevo"
    mv -f "$destino.nuevo" "$destino"
    info "$destino era una versión distribuida sin editar (${vivo:0:12}…): reemplazado; la anterior en $anterior"
    return 0
  fi
  info "$destino fue editado en el host: se conserva (diferencias con el del repo, abajo)"
  diff -u "$destino" "$AQUI/$1" || true
  faltan="$(LC_ALL=C comm -13 <(claves_conf "$destino") <(claves_conf "$AQUI/$1") | tr '\n' ' ')"
  if [[ -n "$faltan" ]]; then
    info "a $destino le faltan claves nuevas (toman el valor de fábrica): $faltan"
  fi
}

esperar_metrica() {   # $1 = archivo .prom, $2 = segundos de espera
  local i
  for (( i = 0; i < $2; i++ )); do
    if [[ -f "$1" ]] && (( $(date +%s) - $(stat -c %Y "$1") <= 5 )); then
      return 0
    fi
    sleep 1
  done
  return 1
}

instalar_guardia() {
  local f
  for f in wtr-guardia-discos wtr-guardia-discos.conf wtr-guardia-discos.service; do
    [[ -f "$AQUI/$f" ]] || morir "falta $AQUI/$f"
  done
  install -o root -g root -m 0755 "$AQUI/wtr-guardia-discos" "$SBIN/wtr-guardia-discos"
  copiar_conf wtr-guardia-discos.conf
  install -o root -g root -m 0644 "$AQUI/wtr-guardia-discos.service" "$UNIDADES/wtr-guardia-discos.service"
  systemctl daemon-reload
  systemctl enable wtr-guardia-discos.service
  systemctl restart wtr-guardia-discos.service
  esperar_metrica "$TEXTFILE/wtr_guardia_discos.prom" 60 || morir 'la guardia no publicó su métrica en 60 s'
  systemctl is-active --quiet wtr-guardia-discos.service || morir 'la guardia no quedó activa'
  info "guardia activa: $(grep -E '^wtr_guardia_discos_(estado|temperatura_max_celsius|simulacro) ' \
    "$TEXTFILE/wtr_guardia_discos.prom" | tr '\n' ' ')"
}

instalar_controlador() {   # $1 = simular | real
  local f h
  for f in wtr-ventiladores wtr-ventiladores.conf wtr-ventiladores.service festivos-madrid.txt; do
    [[ -f "$AQUI/$f" ]] || morir "falta $AQUI/$f"
  done
  h="$(hwmon)" || morir 'no está el sysfs del it87 (name=it8613)'
  [[ -e "$h/pwm3_auto_start" && -e "$h/pwm5_auto_start" ]] \
    || morir "falta pwm3_auto_start o pwm5_auto_start: el DKMS no tiene el parche ($VERSION_DKMS)"
  install -o root -g root -m 0755 "$AQUI/wtr-ventiladores" "$SBIN/wtr-ventiladores"
  install -d -o root -g root -m 0755 "$DATOS"
  install -o root -g root -m 0644 "$AQUI/festivos-madrid.txt" "$DATOS/festivos-madrid.txt"
  copiar_conf wtr-ventiladores.conf
  install -o root -g root -m 0644 "$AQUI/wtr-ventiladores.service" "$UNIDADES/wtr-ventiladores.service"
  if [[ "$1" == simular ]]; then
    install -d -o root -g root -m 0755 "$(dirname "$DROPIN")"
    cat > "$DROPIN" <<'EOT'
# Fase de simulación (spec §9, H3): todo menos escribir en sysfs. Lo quita `instalar.sh instalar`.
[Service]
ExecStart=
ExecStart=/usr/local/sbin/wtr-ventiladores correr --simular
ExecStopPost=
ExecStopPost=/usr/local/sbin/wtr-ventiladores respaldo --simular
EOT
  else
    rm -f "$DROPIN"
    rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
  fi
  systemctl daemon-reload
  systemctl enable wtr-ventiladores.service
  systemctl restart wtr-ventiladores.service
  esperar_metrica "$TEXTFILE/wtr_ventiladores.prom" 90 || morir 'el controlador no publicó su métrica en 90 s'
  systemctl is-active --quiet wtr-ventiladores.service || morir 'el controlador no quedó activo'
  info "controlador activo ($1): $(grep -E '^wtr_ventiladores_(pwm_aplicado|estado\{estado="(ok|simulando)"\})' \
    "$TEXTFILE/wtr_ventiladores.prom" | tr '\n' ' ')"
}

probar_sandbox() {
  local unidad="$UNIDADES/wtr-guardia-discos.service" linea
  local -a props=()
  [[ -f "$unidad" && -x "$SBIN/wtr-guardia-discos" ]] || morir 'primero: bash instalar.sh guardia'
  # Las mismas opciones de endurecimiento que la unidad instalada. RuntimeDirectory propio: el de la
  # guardia se borra al parar una unidad que lo declare, y la guardia está viva.
  while IFS= read -r linea; do
    props+=(-p "$linea")
  done < <(sed -n '/^\[Service\]/,/^\[/p' "$unidad" | grep -E \
    '^(Protect|Private|IPAddressDeny|Restrict|NoNewPrivileges|LockPersonality|MemoryDeny|SystemCall|Device|Capability|ReadWritePaths|StateDirectory)')
  props+=(-p RuntimeDirectory=wtr-guardia-prueba -E WTR_GUARDIA_RUN=/run/wtr-guardia-prueba)
  systemd-run --wait --pipe --collect --quiet --unit=wtr-guardia-prueba-sandbox "${props[@]}" \
    "$SBIN/wtr-guardia-discos" probar-sandbox
}

verificar() {
  local fallos=0 h c edad
  if dkms status it87 2>/dev/null | grep -q "^it87/$VERSION_DKMS, $(uname -r), .*: installed"; then
    info "dkms: it87/$VERSION_DKMS instalado para $(uname -r)"
  else
    echo "FALLO: dkms sin it87/$VERSION_DKMS para $(uname -r)"; fallos=1
  fi
  if h="$(hwmon)"; then
    for c in pwm3 pwm5; do
      [[ -e "$h/${c}_auto_start" ]] || { echo "FALLO: falta $h/${c}_auto_start"; fallos=1; continue; }
      echo "$c: enable=$(cat "$h/${c}_enable") auto_start=$(cat "$h/${c}_auto_start")" \
           "point1=$(cat "$h/${c}_auto_point1_temp") point2=$(cat "$h/${c}_auto_point2_temp")" \
           "point3=$(cat "$h/${c}_auto_point3_temp") slope=$(cat "$h/${c}_auto_slope")"
      [[ "$(cat "$h/${c}_enable")" == 2 ]] || { echo "FALLO: ${c}_enable != 2"; fallos=1; }
    done
  else
    echo 'FALLO: no está el sysfs del it87'; fallos=1
  fi
  for c in wtr-guardia-discos wtr-ventiladores; do
    systemctl is-enabled --quiet "$c.service" || { echo "FALLO: $c no habilitado al arrancar"; fallos=1; }
    systemctl is-active --quiet "$c.service" || { echo "FALLO: $c inactivo"; fallos=1; }
    cmp -s "$AQUI/$c" "$SBIN/$c" || { echo "FALLO: $SBIN/$c distinto del del repo"; fallos=1; }
  done
  [[ -e "$DROPIN" ]] && { echo "FALLO: el controlador sigue en simulación ($DROPIN)"; fallos=1; }
  grep -qE '^ACCION=apagar\s*(#.*)?$' /etc/wtr-guardia-discos.conf \
    || { echo 'FALLO: /etc/wtr-guardia-discos.conf sin ACCION=apagar'; fallos=1; }
  for c in PISO_NOCHE_SATA PISO_NOCHE_SYS CICLOS_CONVERGENCIA; do   # claves de la enmienda rev. 5.1 (§6)
    grep -qE "^[[:space:]]*${c}[[:space:]]*=" /etc/wtr-ventiladores.conf 2>/dev/null \
      || { echo "FALLO: /etc/wtr-ventiladores.conf sin $c (sin migrar: ver el diff de instalar.sh instalar)"; fallos=1; }
  done
  for c in wtr_guardia_discos:90 wtr_ventiladores:180; do
    local prom="$TEXTFILE/${c%%:*}.prom"
    if [[ -f "$prom" ]]; then
      edad=$(( $(date +%s) - $(stat -c %Y "$prom") ))
      (( edad <= ${c##*:} )) || { echo "FALLO: $prom con $edad s"; fallos=1; }
    else
      echo "FALLO: falta $prom"; fallos=1
    fi
  done
  grep -q '^wtr_ventiladores_estado{estado="ok"} 1$' "$TEXTFILE/wtr_ventiladores.prom" 2>/dev/null \
    || { echo 'FALLO: wtr_ventiladores_estado ok != 1'; fallos=1; }
  grep -q '^wtr_guardia_discos_simulacro 0$' "$TEXTFILE/wtr_guardia_discos.prom" 2>/dev/null \
    || { echo 'FALLO: la guardia está en simulacro o sin métrica'; fallos=1; }
  grep -hE '^wtr_(guardia_discos_estado|disco_temperatura_max_celsius|ventiladores_pwm_aplicado)' \
    "$TEXTFILE"/wtr_*.prom 2>/dev/null || true
  if systemctl is-active --quiet guardia-temp-discos.service; then
    echo 'AVISO: la guardia transitoria guardia-temp-discos sigue activa (systemctl stop guardia-temp-discos)'
  fi
  if (( fallos == 0 )); then
    info 'verificación OK'
  else
    morir 'verificación con fallos'
  fi
}

revertir() {
  local bios=0 guardia=0 a f fecha
  for a in "$@"; do
    case "$a" in
      --bios) bios=1 ;;
      --tambien-guardia) guardia=1 ;;
      *) morir "opción desconocida: $a" ;;
    esac
  done
  fecha="$(date +%Y%m%dT%H%M%S)"
  systemctl disable --now wtr-ventiladores.service 2>/dev/null || true   # ExecStopPost: respaldo
  if (( bios )) && [[ -x "$SBIN/wtr-ventiladores" ]]; then
    "$SBIN/wtr-ventiladores" bios
  fi
  rm -f "$UNIDADES/wtr-ventiladores.service" "$DROPIN" "$SBIN/wtr-ventiladores" "$TEXTFILE/wtr_ventiladores.prom"
  rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
  rm -rf "$DATOS"
  [[ -f /etc/wtr-ventiladores.conf ]] && mv /etc/wtr-ventiladores.conf "/etc/wtr-ventiladores.conf.retirado-$fecha"
  if (( guardia )); then
    systemctl disable --now wtr-guardia-discos.service 2>/dev/null || true
    rm -f "$UNIDADES/wtr-guardia-discos.service" "$SBIN/wtr-guardia-discos" "$TEXTFILE/wtr_guardia_discos.prom"
    [[ -f /etc/wtr-guardia-discos.conf ]] && mv /etc/wtr-guardia-discos.conf "/etc/wtr-guardia-discos.conf.retirado-$fecha"
    info 'guardia retirada; /var/lib/wtr-guardia-discos (marcadores de apagado) se conserva'
  fi
  systemctl daemon-reload
  if f="$(hwmon)"; then
    info "revertido: pwm3_auto_start=$(cat "$f/pwm3_auto_start") pwm5_auto_start=$(cat "$f/pwm5_auto_start" 2>/dev/null || echo '?')" \
         "point2 $(cat "$f/pwm3_auto_point2_temp")/$(cat "$f/pwm5_auto_point2_temp")"
  fi
}

case "${1:-}" in
  guardia) instalar_guardia ;;
  probar-sandbox) probar_sandbox ;;
  simular) instalar_guardia; instalar_controlador simular ;;
  instalar) instalar_guardia; instalar_controlador real; verificar ;;
  verificar) verificar ;;
  revertir) shift; revertir "$@" ;;
  *) morir "uso: $0 guardia|probar-sandbox|simular|instalar|verificar|revertir [--bios] [--tambien-guardia]" ;;
esac
