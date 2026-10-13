# wtr-max-fans

Control de ventiladores para el AOOSTAR WTR MAX en Proxmox VE y Debian. Los ventiladores del chasis siguen la temperatura de los discos duros y del NVMe, pueden funcionar más silenciosos de noche, y una guardia independiente vigila la temperatura de los discos. Todo se configura desde un menú de terminal.

> **Pre-release: under development, not ready for use.**
> Pre-lanzamiento: en desarrollo, no está listo para usarse.
> Nada de lo que describe este README está disponible todavía. La instalación y los comandos son el comportamiento previsto para la v1.0 y pueden cambiar antes del lanzamiento.

English: [README.md](README.md)

---

## Por qué existe

El WTR MAX tiene dos problemas de ventiladores que la configuración de fábrica no resuelve:

- **La curva de ventiladores sigue los sensores equivocados.** La curva del firmware reacciona a dos termistores de la placa. En una unidad, los sensores de la placa variaban 1–2 °C mientras los discos variaban 3–8 °C. Los sensores de la placa no siguen la jaula de discos ni el NVMe.
- **El firmware puede detener los ventiladores de la jaula de discos.** Con ventiladores PWM, la curva del firmware puede parar los ventiladores traseros. En la unidad donde se midió esto, los discos llegaron a 56 °C.

El kernel de Linux no controla el chip de esta placa (ITE IT8613E). El driver de la comunidad `frankcrawford/it87` sí lo controla, pero oculta los atributos que este proyecto necesita, así que el proyecto incluye un parche de tres líneas para ese driver. Algunos ventiladores de fábrica ni siquiera son PWM: funcionan a máxima velocidad aunque el firmware pida otra cosa.

`wtr-max-fans` deja el chip en su modo automático propio y solo cambia el punto de temperatura inicial de su curva, usando como entrada la temperatura de los discos y del NVMe. Si el software se detiene, la curva del firmware sigue funcionando.

## Hardware

| Placa | Estado |
|---|---|
| AOOSTAR WTR MAX (producto DMI `WTR MAX`, chip ITE IT8613E en el puerto Super-I/O `0x2E`) | Compatible en v1 |
| Otra placa con el mismo chip IT8613E | Solo en modo experto (ver abajo) |
| Cualquier otro chip Super-I/O | No compatible, nunca |

Canales de ventilador del WTR MAX, medidos en una unidad. La calibración confirma cada uno en tu equipo:

| Canal | Ventilador habitual | Rol en `wtr-max-fans` |
|---|---|---|
| `pwm2` | CPU | No se controla. Sigue la curva del firmware. |
| `pwm3` | SYS, el ventilador inferior bajo el chasis | Controlado, rol `base`. Sigue al disco más caliente y al NVMe. |
| `pwm4` | Conector MEM | Suele ser un ventilador de 2 hilos, sin PWM ni tacómetro. No se controla. |
| `pwm5` | SATA, los ventiladores traseros | Controlado, rol `disk-cage`. Sigue al disco más caliente. |

**Requisitos**

- AOOSTAR WTR MAX.
- Proxmox VE 8 o 9, o Debian 12 o 13 (OpenMediaVault 7 usa Debian 12).
- Ventiladores PWM de 4 pines en los canales que quieras controlar. La calibración revisa cada uno.
- Acceso a internet durante la instalación, para descargar paquetes.

**Modo experto para otras placas con el mismo chip.** Algunas placas usan el mismo chip pero no son un WTR MAX, y sus conectores de ventilador pueden estar cableados de otra forma. El instalador las rechaza, salvo que uses `install --expert-board` o elijas el flujo experto en el menú. El modo experto:

1. Muestra una advertencia y pide que escribas `unsupported`.
2. Instala en **modo solo lectura**: la guardia solo vigila y avisa, y no se escribe nada en los ventiladores.
3. Exige calibración. Tú confirmas cada canal. Si respondes "no estoy seguro", el canal queda sin controlar. No hay mapa por defecto.
4. Activa el control de ventiladores solo después de un paso explícito "Activar control de ventiladores".
5. Marca la instalación como experta en todas partes: en la pantalla de estado, en el asunto de cada alerta, en el informe y en la métrica `wtr_max_fans_board_support{support="expert"}`.

La comprobación del chip nunca se puede saltar.

## Funciones (plan de la v1)

- **Curvas de ventiladores guiadas por los discos.** Los ventiladores de la jaula de discos siguen al disco más caliente. El ventilador base sigue al más caliente entre los discos y el NVMe.
- **Tres perfiles:** `quiet`, `balanced` y `cool`. Los mismos puntos de temperatura y el mismo límite de seguridad en los tres. La tabla está en la siguiente sección.
- **Modo nocturno.** Una franja horaria en hora local, un final opcional más tarde los fines de semana y un archivo opcional de festivos.
- **Guardia térmica.** Un servicio separado que pone los ventiladores a máxima potencia cuando un disco se calienta. El apagado está desactivado salvo que lo actives. Ver [Seguridad](#seguridad).
- **Calibración guiada.** Detecta qué ventilador es cuál y si responde a PWM. Al terminar, restaura los valores del firmware que tocó.
- **Prueba de ruido.** Reproduce el nivel nocturno de cada perfil y pregunta si es aceptable. Después propone el perfil más potente que sea aceptable.
- **Desinstalación limpia.** Restaura los valores del firmware registrados en la calibración y luego elimina las unidades, el módulo y los archivos instalados.
- **Métricas opcionales para Prometheus,** que solo se escriben si ya existe el directorio del collector de textfile.
- **Menú de terminal** (whiptail) y línea de comandos para scripts.

### Perfiles

Los puntos de temperatura son los mismos en todos los perfiles: los ventiladores se quedan en el mínimo hasta 38 °C, alcanzan el valor de día a 46 °C, convergen al valor de día a 50 °C de noche y llegan a máxima velocidad (255) a 52 °C. Los valores son PWM de 0 a 255, con el porcentaje entre paréntesis.

| Parámetro | `quiet` | `balanced` | `cool` |
|---|---|---|---|
| Jaula de discos, mínimo | 102 (40 %) | 115 (45 %) | 128 (50 %) |
| Jaula de discos, día a 46 °C | 128 (50 %) | 153 (60 %) | 191 (75 %) |
| Jaula de discos, noche a 46 °C | 115 (45 %) | 153 (60 %) | 153 (60 %) |
| Base, mínimo | 77 (30 %) | 77 (30 %) | 100 (39 %) |
| Base, día a 46 °C | 77 (30 %) | 100 (39 %) | 128 (50 %) |
| Base, noche a 46 °C | 77 (30 %) | 77 (30 %) | 100 (39 %) |

`balanced` es el punto de confort medido en una unidad: ventiladores traseros al 60 %, ventilador inferior al 30 % de noche, discos entre 33 y 42 °C. `cool` es para habitaciones calurosas. La pantalla de estado sugiere este perfil cuando el máximo de los discos en 24 horas supera los 46 °C.

El ventilador base nunca baja del 30 % en ningún perfil hasta que se verifique un mínimo más bajo con tacómetro. El perfil `custom` te permite fijar cada valor en `config.ini`, dentro de las reglas de seguridad.

A largo plazo, los discos deberían mantenerse entre 30 y 45 °C. El NVMe debería mantenerse por debajo de 60 °C con carga sostenida. El ventilador de la CPU nunca se controla.

### Modo nocturno

El modo nocturno usa la hora local del sistema. Configura `night_start` y `night_end` (por ejemplo 21:00 y 09:00). Puedes añadir un final más tarde los fines de semana y un archivo de festivos. No se incluye ningún archivo de festivos. Si el archivo de festivos no se puede leer, se trata como un día normal, así que los ventiladores usan los valores de día y no mueven menos aire.

## Seguridad

**Lo que el software nunca hace**

- Nunca escribe en un ventilador controlado por debajo del 20 % de PWM (51 de 255).
- El controlador nunca escribe `pwmN` ni cambia el modo del chip. Solo cambia el punto de inicio de la curva propia del chip. La única excepción es el último recurso de la guardia, que se usa únicamente cuando falta el atributo del punto de inicio. Queda fijado hasta que ejecutes `acknowledge`.
- Nunca controla el ventilador de la CPU ni el canal MEM.
- Nunca funciona junto a `fancontrol`, `fan2go` u otro módulo `it87`. El instalador lo rechaza y explica cómo migrar.
- Nunca provoca un apagado a partir del NVMe. El NVMe se limita por sí mismo; el software sube los ventiladores a máxima potencia.
- Nunca apaga el equipo a menos que lo actives.
- Nunca envía datos fuera de la máquina. Los servicios no tienen acceso a la red y no hay telemetría.

**Qué pasa cuando algo falla**

| Fallo | Comportamiento |
|---|---|
| El controlador se cae o se cuelga | El chip conserva el último punto de inicio, que nunca queda por debajo del mínimo. Una copia de seguridad restaura los valores del perfil en 5 segundos. El servicio se reinicia. |
| Falta el driver (tras una actualización del kernel, o con Secure Boot sin registrar la clave) | El controlador se detiene sin escribir nada. Los ventiladores siguen la curva del firmware. La guardia avisa una vez por arranque. |
| Un disco o el NVMe no se puede leer durante tres ciclos | El canal pasa a máxima velocidad. |
| Un ventilador se para mientras su canal está en o por encima del mínimo | El otro canal controlado pasa a máxima velocidad y recibes una alerta. |

**La guardia térmica**

La guardia es un servicio separado. Sigue funcionando aunque el controlador esté detenido.

| Paso | Condición | Acción |
|---|---|---|
| Aire máximo | Un disco llega a 54 °C, o el NVMe llega a 70 °C | Ventiladores al 100 %. Se libera por debajo de 50 °C en los discos. |
| Alerta | Cualquier acción de la guardia | Entrada en el journal, correo a root si existe `sendmail` y un comando de gancho opcional. |
| Apagado (opcional) | Un disco a 58 °C o más en tres lecturas seguidas separadas 30 segundos, **y** un segundo disco a 50 °C o más | Apagado limpio tras un aviso de un minuto. Nunca dentro de los 30 minutos posteriores a un apagado térmico anterior. El NVMe nunca cuenta para esta regla. |

El valor por defecto es **alerta**. El instalador pregunta si quieres activar el apagado automático, explica la regla y la respuesta por defecto es no. En el menú, para activar el apagado hay que escribir `shutdown`. Con un solo disco no es posible apagar: la guardia avisa y el menú lo indica. El NVMe nunca provoca un apagado.

**Secure Boot**

Si Secure Boot está activo, el módulo no está firmado. El instalador lo firma con una clave propia de esta máquina (MOK) y la importa. Confirmas la clave una vez en la pantalla MOK Manager en el siguiente arranque. Hasta entonces, los ventiladores siguen la curva del firmware.

**Otras reglas de seguridad**

- El instalador se niega a continuar mientras haya otro controlador de ventiladores o otro paquete DKMS `it87` activo.
- El código fuente del driver se comprueba contra los hashes del repositorio antes de compilarlo.
- Ambos servicios se ejecutan con privilegios restringidos y una lista cerrada de dispositivos. El controlador nunca escribe si el atributo de punto de inicio del driver no está en todos los canales controlados.

## Instalación (prevista, v1.0)

Instala desde una versión etiquetada, no con una línea `curl | bash`:

```sh
apt install git            # solo si falta git
git clone --branch v1.0.0 --depth 1 https://github.com/phyrexia/wtr-max-fans.git
cd wtr-max-fans
sudo ./install.sh
```

El instalador:

1. Comprueba que la placa y el chip son un WTR MAX compatible. Si no lo son, se detiene con un mensaje claro.
2. Instala los paquetes que necesita: `dkms`, el compilador, las cabeceras del kernel, `smartmontools`, `whiptail` y `mokutil` si Secure Boot está activo.
3. Compila y carga el driver parcheado con DKMS.
4. Instala y arranca la guardia en modo alerta.
5. Abre el asistente de calibración. Muestra qué ventiladores responden, te pide confirmar cada rol, hace la prueba de ruido y pregunta por el apagado automático (por defecto, no).
6. Guarda la calibración y arranca el controlador.

Tiempo esperado en un Proxmox VE 9 nuevo con internet: unos 10 minutos, incluida la compilación de DKMS.

**¿Por qué no `curl | bash`?** El script se ejecuta como root y compila un módulo del kernel. Una copia local se puede leer antes de ejecutarla. Si la conexión se corta, un script enviado por tubería puede ejecutarse a medias. Una copia etiquetada además fija los bytes exactos, y puedes verificar la firma con `git verify-tag`. Para quien no use git hay archivos de release con `SHA256SUMS`.

**Actualizar.** Clona la nueva etiqueta y vuelve a ejecutar `sudo ./install.sh`. Actualiza en el mismo sitio, conserva tu configuración y tu calibración, y recompila el módulo solo si cambió el driver.

## Uso del menú

```sh
sudo wtr-max-fans
```

```
wtr-max-fans 1.0.0 — control de ventiladores AOOSTAR WTR MAX
  1  Estado                temperaturas de ventiladores, discos y NVMe, perfil, protección, driver
  2  Calibrar ventiladores detecta qué ventilador es cuál y si responde (obligatorio una vez)
  3  Perfil                quiet / balanced / cool
  4  Modo nocturno         activar/desactivar, horario, fines de semana, festivos
  5  Protección térmica    alerta (por defecto) / apagado
  6  Prueba de ruido       volver a escuchar los perfiles
  7  Informe               paquete para una incidencia
  8  Desinstalar
  0  Salir
```

Capturas de pantalla: disponibles con la v1.0.

## Línea de comandos (prevista)

Cada acción del menú también es un comando. Usa la línea de comandos para scripts y sesiones remotas. Sin terminal, el menú se niega a funcionar y muestra el comando equivalente.

| Comando | Para qué sirve |
|---|---|
| `wtr-max-fans check` | Comprobaciones de solo lectura: placa, chip, driver y software en conflicto. No escribe nada. |
| `wtr-max-fans calibrate [--yes]` | Calibración guiada. `--yes` mantiene los valores por defecto y omite la prueba de ruido. |
| `wtr-max-fans profile quiet\|balanced\|cool` | Cambia el perfil. |
| `wtr-max-fans night on\|off [--start HH:MM] [--end HH:MM]` | Modo nocturno. |
| `wtr-max-fans protection alert\|shutdown` | Cambia la acción de la guardia. |
| `wtr-max-fans status [--json]` | Estado en texto o en JSON. |
| `wtr-max-fans acknowledge` | Limpia el estado de emergencia fijado, después de corregir la causa. |
| `wtr-max-fans restore-firmware` | Devuelve los valores del firmware registrados en la calibración, sin desinstalar. |
| `wtr-max-fans report` | Crea el paquete para una incidencia. |
| `wtr-max-fans uninstall [--yes] [--purge]` | Elimina el software. |

### Códigos de salida

| Código | Significado |
|---|---|
| 3 | No es un WTR MAX reconocido, o el chip no es IT8613E. No se instaló nada. |
| 4 | El driver se cargó, pero el parche no está activo. El instalador elimina lo que añadió. |
| 5 | Hay otro controlador de ventiladores o otro paquete DKMS `it87`. El mensaje explica la migración. |
| 6 | Falta un requisito. Se muestra el comando `apt` exacto. |
| 78 | Servicio controlador: falta el driver o su atributo. No se escribió nada. |

## Métricas (opcional)

Si existe el directorio del collector de textfile (por ejemplo `/var/lib/prometheus/node-exporter`), los servicios escriben allí métricas `wtr_max_fans_*`: temperaturas de discos y NVMe, valores PWM objetivo y aplicados por canal y rol, modo, estado y estado de la guardia. `contrib/prometheus/` contiene reglas de alerta de ejemplo con sus pruebas. No se instalan automáticamente. `wtr-max-fans status --json` ofrece los mismos datos sin Prometheus.

## Preguntas frecuentes

**¿Mis ventiladores son PWM?**
Los ventiladores PWM tienen cuatro pines. La calibración prueba cada canal: comprueba que el ventilador se frena cuando el chip lo pide y que el tacómetro lee una velocidad. Un ventilador que no responde se informa como tal y no se toca. Para tener control, cámbialo por un ventilador PWM de 4 pines.

**¿Por qué no `fancontrol`?**
`fancontrol` usa el modo manual del chip. Si el proceso se mata, el chip conserva su último ciclo de trabajo. No puede leer la temperatura de los discos sin una entrada hwmon, no gestiona los discos en reposo y no tiene modo nocturno. Tampoco puede usar la curva automática del chip con un punto de inicio, que es lo que este proyecto necesita.

**¿Qué pasa cuando actualizo el kernel?**
DKMS recompila el driver para el nuevo kernel si están instaladas sus cabeceras. Tras una actualización del kernel, ejecuta `sudo wtr-max-fans check`. Si el kernel en uso no tiene módulo, los ventiladores vuelven a la curva del firmware. La guardia registra una alerta y envía un correo a root una vez por arranque.

**¿Hacia dónde soplan los ventiladores?**
Los ventiladores traseros expulsan aire fuera del chasis. En los ventiladores Noctua, el lado con la etiqueta es la salida de aire. Consulta las [preguntas frecuentes de Noctua](https://www.noctua.at/en/support/faqs/how-do-i-determine-my-fans-airflow-direction) para más detalles sobre la orientación.

**¿Y la presión positiva?**
Este software no controla la presión. El chasis está pensado para tomar aire por el frente, pasarlo por los discos y expulsarlo por los ventiladores traseros. Deja los ventiladores como están instalados; no hace falta darles la vuelta.

**¿TrueNAS o Unraid?**
Todavía no. La v1 apunta a Proxmox VE y Debian, incluido OpenMediaVault. Otras bases quedan fuera del alcance de la v1.

## Desinstalación (prevista)

Usa el elemento 8 del menú, o:

```sh
sudo wtr-max-fans uninstall
```

La desinstalación detiene el controlador, restaura los valores del firmware registrados en la calibración y elimina los servicios, el programa, el módulo DKMS, la entrada de carga del módulo, el archivo de métricas y el directorio de estado. Conserva `/etc/wtr-max-fans/` (tu configuración y calibración) salvo que uses `--purge`. Deja instalados los paquetes de apt y los muestra en pantalla para que los quites tú. No hace falta reiniciar: el chip vuelve de inmediato a los valores propios del firmware.

## Contribuir y reportar una placa

- **Reportar una placa o un problema.** Ejecuta `sudo wtr-max-fans report` (previsto) y adjunta el paquete a la incidencia. Las incidencias deben incluir este paquete. Contiene las cadenas DMI, `dkms status`, el listado de hwmon, un escaneo de discos, un resumen de temperaturas, los dos archivos de configuración, las últimas 500 líneas del journal y la versión. No incluye números de serie. Las líneas del journal pueden incluir el nombre de tu equipo, así que revisa el archivo antes de publicarlo.
- **Seguridad.** Reporta las vulnerabilidades por el canal privado de GitHub. Consulta `SECURITY.md`.
- **Contribuciones.** Lee `CONTRIBUTING.md`. Las pruebas deben pasar en local y en CI, y cada cambio funcional va en su propio commit.

## Desarrollo

```sh
python3 -m pytest -q tests
```

Las pruebas usan una raíz de sistema simulada y no necesitan hardware. El CI ejecuta shellcheck, pytest con Python 3.11 y 3.13, `promtool` sobre `contrib/prometheus/`, la compilación del driver contra las cabeceras de Debian 12, Debian 13 y Proxmox VE actual, y `reuse lint`.

## Licencia

- `driver/` es **GPL-2.0-only**. Deriva del driver `it87` de Linux.
- Todo lo demás es **MIT**.
- Cada archivo lleva cabeceras SPDX, y los textos completos están en `LICENSES/` (formato REUSE).

El driver y el controlador se ejecutan como procesos separados y solo comparten sysfs.

## Créditos

- El driver es [`frankcrawford/it87`](https://github.com/frankcrawford/it87), fijado en el commit `bd7a566c`, con un parche de este proyecto.
- Ideas y antecedentes: [fan2go](https://github.com/markusressel/fan2go), [hddfancontrol](https://github.com/desbma/hddfancontrol), [UGREEN-Fan-Control](https://github.com/0n1cOn3/UGREEN-Fan-Control) y `pwmconfig` de lm-sensors.

## Marcas

No está afiliado ni respaldado por AOOSTAR. AOOSTAR y WTR MAX son marcas de sus respectivos titulares.
