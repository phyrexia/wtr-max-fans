# SPDX-FileCopyrightText: 2026 Gabriel González Pineda <https://github.com/phyrexia>
# SPDX-License-Identifier: Apache-2.0
"""Pruebas de wtr-ventiladores (spec 2026-10-13 §8.1, casos 1 a 12 y 17, y enmienda rev. 5.1 §7.1, modo
nocturno): sysfs simulado que registra cada escritura, smartctl falso en PATH, reloj inyectable y archivo
de escalón con mtime controlable.

Correr desde la raíz del repositorio (no tocan el hardware: sysfs, smartctl y systemd son dobles):
  python3 -m unittest discover -s v0 -p 'test_*.py' -v
"""

import datetime as dt
import errno
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from zoneinfo import ZoneInfo

AQUI = Path(__file__).resolve().parent
_cargador = importlib.machinery.SourceFileLoader("wtr_ventiladores", str(AQUI / "wtr-ventiladores"))
_spec = importlib.util.spec_from_loader("wtr_ventiladores", _cargador)
wv = importlib.util.module_from_spec(_spec)
sys.modules["wtr_ventiladores"] = wv  # dataclasses lo busca ahí
_cargador.exec_module(wv)

MAD = ZoneInfo("Europe/Madrid")
CFG = wv.Config()
DIA = dt.datetime(2026, 10, 14, 12, 0, tzinfo=MAD)      # miércoles laborable, mediodía
NOCHE = dt.datetime(2026, 10, 14, 23, 0, tzinfo=MAD)

ESTADO_BIOS = {"enable": 2, "auto_start": 100, "auto_point1_temp": 0, "auto_point2_temp": 20000,
               "auto_point3_temp": 80000, "auto_slope": 0, "auto_point1_temp_hyst": -1000}

# wtr-ventiladores.conf tal como se distribuyó en bbf1050 (rev. 4, instalado en el host el 2026-10-13).
CONF_REV4 = """\
# /etc/wtr-ventiladores.conf: lo relee wtr-ventiladores en cada ciclo (60 s), sin reiniciar.
# El instalador lo copia sólo si no existe: los cambios hechos en el host se conservan.
# Un valor inválido usa el de fábrica (métrica wtr_ventiladores_config_ok=0). Si la curva no es
# coherente (temperaturas crecientes, PISO <= NOCHE <= SILENCIO), la curva entera vuelve a fábrica.

# auto = calendario de p3-perfil-cpu (noche 21:00-09:00, 10:00 fines de semana y festivos de
# Madrid); dia o noche lo fuerzan.
MODO=auto
CICLO_S=60

# Temperatura máxima de los HDD (°C): hasta T_PISO piso; rampa hasta T_SILENCIO; de noche, rampa
# hasta coincidir con la curva diurna en T_CONVERGE; desde T_MAX, 255.
T_PISO_C=38
T_SILENCIO_C=46
T_CONVERGE_C=50
T_MAX_C=52

# Suavizado: sube como mucho PASO_SUBIDA por ciclo (salvo objetivo 255); baja PASO_BAJADA por ciclo
# tras CICLOS_BAJADA ciclos con el objetivo BANDA_MUERTA o más por debajo.
PASO_SUBIDA=20
PASO_BAJADA=4
BANDA_MUERTA=6
CICLOS_BAJADA=3

# SATA (pwm5): enfría la jaula de los seis HDD. 153 = 60 %, el punto cómodo del Owner.
PISO_SATA=115
SILENCIO_SATA=153
NOCHE_SATA=153
RESPALDO_SATA=153

# SYS (pwm3): el ventilador de la base (Owner, 2026-10-13). En v1 sigue a los discos con el piso
# y el nivel nocturno en 77 (30 %, ~730 rpm, sin queja). Bajarlos a 64 o 51 sólo si la calibración
# muestra rpm estables (tabla de calibración del README). Pendiente v1.1: seguir al NVMe.
PISO_SYS=77
SILENCIO_SYS=100
NOCHE_SYS=77
RESPALDO_SYS=77

# Vacío = descubrimiento automático de los HDD ATA rotacionales. Para pruebas: rutas /dev/...
DISCOS=
"""


def madrid(texto):
    return dt.datetime.fromisoformat(texto).replace(tzinfo=MAD)


def ok(t):
    return wv.Lectura("ok", t)


def discos(n=6):
    return [wv.Disco(f"ata-DISCO{i}", f"sd{chr(97 + i)}", f"/dev/sd{chr(97 + i)}", "MODELO X") for i in range(n)]


def lecturas(*temps):
    return {f"ata-DISCO{i}": (t if isinstance(t, wv.Lectura) else ok(t)) for i, t in enumerate(temps)}


class Banco(unittest.TestCase):
    """Sysfs simulado del IT8613E (hwmon6) más los archivos de run, escalón y métrica en un tmp.

    El «kernel» simulado envuelve cada escritura como el it87: registra (attr, valor), con enable=0
    pone base+3 (auto_start) en 255 y al pasar a 2 exige Off <= Start <= Full (check_trip_points).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.hw = raiz / "sys" / "hwmon6"
        self.hw.mkdir(parents=True)
        (self.hw / "name").write_text("it8613\n")
        self.crear_canal("pwm3", auto_start=True)
        self.crear_canal("pwm5", auto_start=True)
        (self.hw / "fan3_input").write_text("730\n")
        (self.hw / "fan5_input").write_text("1190\n")
        self.escritos = []
        self.viejos = {k: getattr(wv, k) for k in ("HWMON_GLOB", "METRICA", "RUN", "ESCALON", "CONF",
                                                    "FESTIVOS", "_escribir_archivo", "SYSTEMCTL",
                                                    "SMARTCTL", "LSBLK", "BYID")}
        wv.HWMON_GLOB = str(raiz / "sys" / "hwmon*")
        wv.METRICA = raiz / "textfile" / "wtr_ventiladores.prom"
        wv.RUN = raiz / "run"
        wv.ESCALON = raiz / "run-guardia" / "escalon"
        wv.CONF = raiz / "wtr-ventiladores.conf"          # no existe: valores de fábrica
        wv.FESTIVOS = AQUI / "festivos-madrid.txt"
        original = self.viejos["_escribir_archivo"]

        def kernel(ruta, valor):
            self.escritos.append((ruta.name, int(valor)))
            if ruta.name.endswith("_enable") and int(valor) == 2:
                c = ruta.name.split("_")[0]
                p = [self.leer(f"{c}_auto_point{i}_temp") for i in (1, 2, 3)]
                if not p[0] <= p[1] <= p[2]:
                    raise OSError(errno.EINVAL, "Invalid argument")
            original(ruta, valor)
            if ruta.name.endswith("_enable") and int(valor) == 0:
                c = ruta.name.split("_")[0]
                if (self.hw / f"{c}_auto_start").exists():
                    (self.hw / f"{c}_auto_start").write_text("255\n")
        wv._escribir_archivo = kernel

    def tearDown(self):
        for k, v in self.viejos.items():
            setattr(wv, k, v)
        self.tmp.cleanup()

    def crear_canal(self, c, auto_start=True, **valores):
        estado = dict(ESTADO_BIOS, **valores)
        for attr, v in estado.items():
            if attr == "auto_start" and not auto_start:
                continue
            (self.hw / f"{c}_{attr}").write_text(f"{v}\n")
        (self.hw / c).write_text(f"{estado['auto_start']}\n")

    def leer(self, attr):
        return int((self.hw / attr).read_text().strip())

    def poner(self, attr, valor):
        (self.hw / attr).write_text(f"{valor}\n")

    def escalon(self, edad_s):
        wv.ESCALON.parent.mkdir(parents=True, exist_ok=True)
        wv.ESCALON.write_text("1\n")
        t = time.time() - edad_s
        os.utime(wv.ESCALON, (t, t))

    def hwmon(self, simular=False):
        raiz = wv.resolver_hwmon()
        return wv.Hwmon(raiz, simular) if raiz else None

    def control(self, simular=False, cfg=CFG):
        return wv.Control(cfg=cfg, simular=simular)

    def preparado(self, simular=False, cfg=CFG):
        ctl = self.control(simular, cfg)
        wv.preparar(ctl, self.hwmon(simular))
        self.escritos.clear()
        return ctl

    def un_ciclo(self, ctl, temps, ahora=DIA, simular=False):
        n = len(temps)
        wv.ciclo(ctl, self.hwmon(simular), ahora, discos(n), lecturas(*temps))
        return ctl

    def callar(self):
        return redirect_stdout(io.StringIO())


# --- 1 y 2: invariante base+3 y preparar ----------------------------------------------------------

class Preparar(Banco):
    def test_sin_pwm5_auto_start_sale_78_sin_ninguna_escritura(self):
        (self.hw / "pwm5_auto_start").unlink()
        with self.callar():
            self.assertEqual(wv.main(["correr"]), 78)
        self.assertEqual(self.escritos, [])
        m = wv.METRICA.read_text()
        self.assertIn('wtr_ventiladores_estado{estado="sin_driver"} 1', m)

    def test_sin_sysfs_sale_78_sin_escrituras(self):
        (self.hw / "name").write_text("nct6775\n")
        with self.callar():
            self.assertEqual(wv.main(["correr"]), 78)
        self.assertEqual(self.escritos, [])

    def test_desde_la_bios_escribe_start_0_y_respaldo_en_orden_solo_lo_que_difiere(self):
        ctl = self.control()
        with self.callar():
            wv.preparar(ctl, self.hwmon())
        self.assertEqual(self.escritos, [("pwm3_auto_point2_temp", 0), ("pwm3_auto_start", 77),
                                         ("pwm5_auto_point2_temp", 0), ("pwm5_auto_start", 153)])
        self.assertEqual(ctl.aplicado, {"pwm3": 77, "pwm5": 153})

    def test_curva_ajena_entera_se_escribe_off_start_full_pendiente_y_luego_automatico(self):
        self.crear_canal("pwm5", auto_point1_temp=30000, auto_point2_temp=40000, auto_point3_temp=70000,
                         auto_slope=24, enable=1)
        with self.callar():
            wv.preparar(self.control(), self.hwmon())
        p5 = [e for e in self.escritos if e[0].startswith("pwm5")]
        self.assertEqual(p5, [("pwm5_auto_point1_temp", 0), ("pwm5_auto_point2_temp", 0),
                              ("pwm5_auto_point3_temp", 80000), ("pwm5_auto_slope", 0),
                              ("pwm5_enable", 2), ("pwm5_auto_start", 153)])

    def test_segunda_vez_no_escribe_nada(self):
        self.preparado()
        with self.callar():
            wv.preparar(self.control(), self.hwmon())
        self.assertEqual(self.escritos, [])


class InvarianteBase3(Banco):
    def test_nunca_pwmN_ni_enable_0_o_1_en_ningun_escenario(self):
        ctl = self.control()
        with self.callar():
            wv.preparar(ctl, self.hwmon())
            for t in (30, 40, 46, 50, 52, 60, 45, 38):
                self.un_ciclo(ctl, [t] * 6)
            self.poner("pwm5_enable", 1)                     # alguien lo pasó a manual
            self.poner("pwm3_auto_point2_temp", 20000)       # o la BIOS tras un reinicio
            self.un_ciclo(ctl, [44] * 6)
            for _ in range(4):                               # ciego
                self.un_ciclo(ctl, [wv.Lectura("fallo")] * 6)
            self.poner("fan5_input", 0)                      # ventilador parado
            for _ in range(4):
                self.un_ciclo(ctl, [44] * 6)
            self.escalon(10)                                 # guardia en escalón
            self.un_ciclo(ctl, [55] * 6)
        self.assertTrue(self.escritos)
        for attr, valor in self.escritos:
            self.assertIsNone(re.fullmatch(r"pwm\d", attr), attr)
            if attr.endswith("_enable"):
                self.assertEqual(valor, 2, attr)
            self.assertTrue(attr.startswith(("pwm3_", "pwm5_")), attr)


# --- 3: sin parche, guardia en emergencia y escalón -----------------------------------------------

class SinParcheYEscalon(Banco):
    def respaldo(self):
        with self.callar():
            return wv.main(["respaldo"])

    def test_sin_auto_start_y_enable_0_de_la_guardia_nadie_escribe_en_ningun_canal(self):
        (self.hw / "pwm5_auto_start").unlink()
        self.poner("pwm5_enable", 0)
        with self.callar(), self.assertRaises(wv.SinDriver):
            wv.preparar(self.control(), self.hwmon())
        self.assertEqual(self.respaldo(), 0)
        with self.callar():
            self.assertEqual(wv.main(["correr"]), 78)
        self.assertEqual(self.escritos, [])
        self.assertEqual(self.leer("pwm5_enable"), 0)

    def test_escalon_vigente_ni_preparar_ni_respaldo_escriben(self):
        self.escalon(30)
        ctl = self.control()
        with self.callar():
            wv.preparar(ctl, self.hwmon())
        self.assertTrue(ctl.escalon)
        self.assertEqual(self.respaldo(), 0)
        self.assertEqual(self.escritos, [])

    def test_escalon_caducado_respaldo_escribe_153_y_77(self):
        self.escalon(120)
        self.assertEqual(self.respaldo(), 0)
        self.assertEqual(sorted(self.escritos), [("pwm3_auto_start", 77), ("pwm5_auto_start", 153)])

    def test_vuelve_el_parche_preparar_devuelve_el_canal_a_automatico_y_respaldo(self):
        self.poner("pwm5_enable", 0)
        self.poner("pwm5_auto_start", 255)
        self.poner("pwm5_auto_point2_temp", 0)
        self.poner("pwm3_auto_point2_temp", 0)
        self.poner("pwm3_auto_start", 77)
        with self.callar():
            wv.preparar(self.control(), self.hwmon())
        self.assertEqual(self.escritos, [("pwm5_enable", 2), ("pwm5_auto_start", 153)])
        self.assertEqual((self.leer("pwm5_enable"), self.leer("pwm5_auto_start")), (2, 153))

    def test_el_bucle_tambien_corrige_enable_cuando_vuelve_el_parche(self):  # N4-M3
        ctl = self.preparado()
        self.poner("pwm5_enable", 0)                       # la guardia lo dejó en manual de emergencia
        self.poner("pwm5_auto_start", 255)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertIn(("pwm5_enable", 2), self.escritos)
        self.assertEqual(self.leer("pwm5_enable"), 2)
        self.assertEqual(ctl.aplicado["pwm5"], 255)        # sigue desde lo leído y baja en rampa
        with self.callar():
            for _ in range(5):
                self.un_ciclo(ctl, [40] * 6)
        self.assertEqual(ctl.aplicado["pwm5"], 255 - 4 * 4)      # 2 ciclos más de espera y 4 bajadas

    def test_la_precondicion_va_al_principio_del_ciclo(self):  # N4-M1
        ctl = self.preparado()
        self.poner("pwm5_enable", 1)
        (self.hw / "pwm5_auto_start").unlink()
        with self.callar(), self.assertRaises(wv.SinDriver):
            self.un_ciclo(ctl, [44] * 6)
        self.assertEqual(self.escritos, [])                 # ni la corrección de enable en pwm5 ni pwm3


# --- 4: curvas -----------------------------------------------------------------------------------

def piso(c, noche, cfg=CFG):
    return cfg.canal(c, "PISO_NOCHE" if noche else "PISO")


class Curvas(unittest.TestCase):
    TABLA = {  # T: (SATA día, SATA noche, SYS día, SYS noche), enmienda rev. 5.1 §2.3
        38: (115, 89, 77, 64), 42: (134, 109, 89, 71), 44: (144, 118, 94, 74), 46: (153, 128, 100, 77),
        48: (187, 175, 152, 140), 50: (221, 221, 203, 203), 52: (255, 255, 255, 255),
    }

    def test_valores_exactos_de_la_tabla(self):
        for t, (sd, sn, yd, yn) in self.TABLA.items():
            self.assertEqual(wv.curva(t, "pwm5", False, CFG), sd, f"SATA día {t}")
            self.assertEqual(wv.curva(t, "pwm5", True, CFG), sn, f"SATA noche {t}")
            self.assertEqual(wv.curva(t, "pwm3", False, CFG), yd, f"SYS día {t}")
            self.assertEqual(wv.curva(t, "pwm3", True, CFG), yn, f"SYS noche {t}")

    def test_bajo_el_piso_y_sobre_el_maximo(self):
        for c, piso_dia, piso_noche in (("pwm5", 115, 89), ("pwm3", 77, 64)):
            self.assertEqual(wv.curva(10, c, False, CFG), piso_dia)
            self.assertEqual(wv.curva(10, c, True, CFG), piso_noche)
            for noche in (False, True):
                self.assertEqual(wv.curva(60, c, noche, CFG), 255)

    def test_continuidad_monotonia_y_piso(self):
        for c in ("pwm5", "pwm3"):
            self.assertEqual(wv.curva(50, c, True, CFG), wv.curva(50, c, False, CFG))
            for noche in (False, True):
                previo = 0
                for decimas in range(300, 600):
                    v = wv.curva(decimas / 10, c, noche, CFG)
                    self.assertGreaterEqual(v, previo, (c, noche, decimas))
                    self.assertGreaterEqual(v, piso(c, noche))
                    previo = v
                for decimas in range(500, 600):   # desde 50 °C día y noche coinciden
                    self.assertEqual(wv.curva(decimas / 10, c, True, CFG), wv.curva(decimas / 10, c, False, CFG))

    def test_noche_estrictamente_bajo_dia_hasta_converge(self):
        """Estricta para T entera 30-49 (T_ctrl es entero). En décimas, <=: SATA a 49,9 °C da 218,7 y 219,3,
        las dos 219, un artefacto de la evaluación en flotantes y del redondeo, no de la ley (§2.1)."""
        for c in ("pwm5", "pwm3"):
            for t in range(30, 50):
                self.assertLess(wv.curva(t, c, True, CFG), wv.curva(t, c, False, CFG), (c, t))
            for decimas in range(300, 500):
                self.assertLessEqual(wv.curva(decimas / 10, c, True, CFG), wv.curva(decimas / 10, c, False, CFG),
                                     (c, decimas))
            for decimas in range(500, 601):
                self.assertEqual(wv.curva(decimas / 10, c, True, CFG), wv.curva(decimas / 10, c, False, CFG),
                                 (c, decimas))

    def test_pendiente_nocturna_sata_bajo_paso_subida(self):
        """La rampa nocturna 46-50 °C, al ritmo máximo medido (0,6 °C/min), pide menos que PASO_SUBIDA."""
        for c in ("pwm5", "pwm3"):
            dia50 = wv.curva(CFG["T_CONVERGE_C"], c, False, CFG)
            pendiente = (dia50 - CFG.canal(c, "NOCHE")) / (CFG["T_CONVERGE_C"] - CFG["T_SILENCIO_C"])
            self.assertLessEqual(pendiente * 0.6, CFG["PASO_SUBIDA"], c)


# --- 5: suavizado --------------------------------------------------------------------------------

def serie(temps, c="pwm3", noche=True, inicio=None, cfg=CFG):
    """Recorre la ley sobre temperaturas (float) y devuelve [(objetivo, aplicado)]."""
    actual = wv.curva(temps[0], c, noche, cfg) if inicio is None else inicio
    bajo, res = 0, []
    for t in temps:
        obj = max(wv.curva(t, c, noche, cfg), piso(c, noche, cfg))
        actual, bajo = wv.suavizar(obj, actual, bajo, cfg)
        res.append((obj, actual))
    return res


def cambios(res):
    aplicados = [a for _, a in res]
    return [(i, b - a) for i, (a, b) in enumerate(zip(aplicados, aplicados[1:]), 1) if a != b]


class Suavizado(unittest.TestCase):
    def test_paso_de_subida_20_y_255_entero(self):
        self.assertEqual(wv.suavizar(200, 100, 0, CFG), (120, 0))
        self.assertEqual(wv.suavizar(255, 100, 0, CFG), (255, 0))
        self.assertEqual(wv.suavizar(110, 100, 0, CFG), (110, 0))

    def test_banda_muerta_y_tres_ciclos_para_bajar_de_a_4(self):
        self.assertEqual(wv.suavizar(150, 153, 0, CFG), (153, 1))       # dentro de la banda: no baja, cuenta
        a, b = wv.suavizar(140, 153, 0, CFG)
        self.assertEqual((a, b), (153, 1))
        a, b = wv.suavizar(140, a, b, CFG)
        self.assertEqual((a, b), (153, 2))
        a, b = wv.suavizar(140, a, b, CFG)
        self.assertEqual((a, b), (149, 10))                              # empezó a bajar: salta a 10
        a, b = wv.suavizar(140, a, b, CFG)
        self.assertEqual(a, 145)
        a, b = wv.suavizar(140, a, b, CFG)
        self.assertEqual(a, 141)
        a, b = wv.suavizar(140, a, b, CFG)
        self.assertEqual(a, 140)                                          # llega al objetivo
        self.assertEqual(wv.suavizar(140, a, b, CFG), (140, 0))

    def test_hueco_menor_que_la_banda_converge_tras_diez_ciclos(self):  # D13
        a, b = 139, 0
        for n in range(1, 10):
            a, b = wv.suavizar(134, a, b, CFG)
            self.assertEqual((a, b), (139, n))
        a, b = wv.suavizar(134, a, b, CFG)
        self.assertEqual(a, 135)
        a, b = wv.suavizar(134, a, b, CFG)
        self.assertEqual(a, 134)
        self.assertEqual(wv.suavizar(134, a, b, CFG), (134, 0))

    def test_el_contador_se_reinicia_cuando_el_objetivo_alcanza_al_aplicado(self):
        a, b = 139, 0
        for _ in range(9):
            a, b = wv.suavizar(134, a, b, CFG)
        self.assertEqual((a, b), (139, 9))
        a, b = wv.suavizar(139, a, b, CFG)
        self.assertEqual((a, b), (139, 0))
        for _ in range(9):
            a, b = wv.suavizar(134, a, b, CFG)
        self.assertEqual((a, b), (139, 9))

    def test_oscilacion_44_45_durante_30_ciclos_cambia_como_mucho_una_vez(self):
        """Precondición (enmienda rev. 5.1 §3): la permanencia continua en el nivel bajo es menor que
        CICLOS_CONVERGENCIA (10 ciclos); el objetivo alcanza al aplicado antes y el contador vuelve a 0."""
        for c in ("pwm5", "pwm3"):
            for noche in (False, True):
                res = serie([44 + (i % 2) for i in range(30)], c, noche)
                self.assertLessEqual(len(cambios(res)), 1, (c, noche))

    def test_oscilacion_lenta_44_45_periodo_seis_no_cambia(self):
        """Mesetas de 3 ciclos < CICLOS_CONVERGENCIA: como mucho la subida inicial."""
        for c in ("pwm5", "pwm3"):
            for noche in (False, True):
                res = serie(([44] * 3 + [45] * 3) * 5, c, noche)
                self.assertLessEqual(len(cambios(res)), 1, (c, noche))

    def test_meseta_de_diez_ciclos_en_el_nivel_bajo_baja_y_sube_una_vez_por_periodo(self):
        """Fuera de la precondición (mesetas de 12 >= 10 ciclos), aceptado: la subida inicial, una bajada de
        <= 5 PWM en 1-2 pasos desde el ciclo 33 y la subida del ciclo 36; nada audible."""
        for c in ("pwm5", "pwm3"):
            for noche in (False, True):
                ch = cambios(serie(([44] * 12 + [45] * 12) * 2, c, noche))
                self.assertLessEqual(len(ch), 4, (c, noche, ch))
                self.assertTrue(all(abs(d) <= 5 for _, d in ch), (c, noche, ch))
                bajadas = [i for i, d in ch if d < 0]
                self.assertTrue(all(i >= 33 for i in bajadas), (c, noche, ch))
                inicios = [i for i in bajadas if i - 1 not in bajadas]   # una bajada = pasos seguidos
                self.assertTrue(all(b - a >= 20 for a, b in zip(inicios, inicios[1:])), (c, noche, ch))
                self.assertLessEqual(sum(-d for _, d in ch if d < 0), 5, (c, noche, ch))
        self.assertEqual(cambios(serie(([44] * 12 + [45] * 12) * 2, "pwm5", True)),
                         [(12, 5), (33, -4), (34, -1), (36, 5)])           # SATA noche: 118 <-> 123

    def test_noche_sata_46_a_50_al_ritmo_maximo_sigue_al_objetivo_sin_retraso(self):
        temps = [46 + 0.6 * k for k in range(8)]          # 7 ciclos a 0,6 °C/min
        res = serie(temps, "pwm5", True)
        aplicados = [a for _, a in res]
        self.assertEqual(aplicados[0], 128)
        self.assertTrue(all(b - a <= 20 for a, b in zip(aplicados, aplicados[1:])), aplicados)
        self.assertEqual(res[-1][1], res[-1][0])

    def test_la_noche_baja_del_piso_diurno(self):
        """T 36 de noche: objetivo y aplicado llegan a 89/64, no a 115/77 (decidir_objetivos usa el piso
        del modo)."""
        ctl = wv.Control(cfg=CFG)
        ctl.modo, ctl.t_ctrl, ctl.ultimo_t = "noche", 36, 36
        ctl.aplicado = {"pwm3": 77, "pwm5": 115}
        for _ in range(20):
            wv.decidir_objetivos(ctl, {"pwm3": 511, "pwm5": 708})
            for c in wv.CANALES:
                ctl.aplicado[c], ctl.ciclos_bajo[c] = wv.suavizar(ctl.objetivo[c], ctl.aplicado[c],
                                                                  ctl.ciclos_bajo[c], CFG)
        self.assertEqual(ctl.objetivo, {"pwm3": 64, "pwm5": 89})
        self.assertEqual(ctl.aplicado, {"pwm3": 64, "pwm5": 89})
        self.assertEqual(ctl.objetivo_dia, {"pwm3": 77, "pwm5": 115})

    def test_noche_sys_46_a_50_al_ritmo_maximo_sigue_al_objetivo_sin_retraso(self):
        temps = [46 + 0.6 * k for k in range(8)]          # 7 ciclos a 0,6 °C/min
        res = serie(temps, "pwm3", True)
        aplicados = [a for _, a in res]
        self.assertEqual(aplicados[0], 77)
        self.assertTrue(all(b - a <= 20 for a, b in zip(aplicados, aplicados[1:])), aplicados)
        self.assertEqual(res[-1][1], res[-1][0])

    def test_noche_sys_46_a_52_sin_cambio_mayor_a_20_hasta_52(self):
        temps = [46 + 0.6 * k for k in range(11)]         # 46 -> 52
        res = serie(temps, "pwm3", True)
        for (o1, a1), (o2, a2) in zip(res, res[1:]):
            if o2 < 255:
                self.assertLessEqual(a2 - a1, 20, res)
        self.assertEqual(res[-1], (255, 255))

    def test_enfriamiento_50_a_46_baja_como_mucho_4_por_ciclo(self):
        res = serie([50 - 0.1 * k for k in range(41)] + [46] * 40, "pwm3", True, inicio=203)
        aplicados = [203] + [a for _, a in res]
        self.assertTrue(all(0 <= a - b <= 4 for a, b in zip(aplicados, aplicados[1:])), aplicados)
        self.assertEqual(aplicados[-1], 77)

    def test_contraste_46_a_50_en_dos_ciclos_sube_de_a_20_sin_pasar_por_255(self):
        res = serie([46, 48, 50, 50, 50, 50, 50, 50, 50], "pwm3", True)
        aplicados = [a for _, a in res]
        self.assertEqual(aplicados[:4], [77, 97, 117, 137])
        self.assertNotIn(255, aplicados)
        self.assertEqual(aplicados[-1], 203)

    def test_lecturas_enteras_al_ritmo_maximo_sin_cambio_mayor_a_20(self):
        temps = [46, 46, 47, 47, 48, 49, 49, 50, 50, 50]  # enteros de smartctl a 0,6 °C/min
        res = serie(temps, "pwm3", True)
        aplicados = [a for _, a in res]
        self.assertTrue(all(b - a <= 20 for a, b in zip(aplicados, aplicados[1:])), aplicados)
        self.assertEqual(aplicados[-1], 203)


class ObjetivoMaximoEnElCiclo(Banco):
    def test_t52_ciego_escalon_y_parado_aplican_255_de_golpe(self):
        with self.callar():
            ctl = self.preparado()
            self.un_ciclo(ctl, [52] * 6)
            self.assertEqual(ctl.aplicado, {"pwm3": 255, "pwm5": 255})
            self.assertEqual(ctl.objetivo_dia, {"pwm3": 255, "pwm5": 255})
            ctl = self.preparado()
            for _ in range(3):
                self.un_ciclo(ctl, [wv.Lectura("fallo")] * 6, ahora=NOCHE)
            self.assertEqual(ctl.aplicado, {"pwm3": 255, "pwm5": 255})
            self.assertEqual(ctl.objetivo_dia, {"pwm3": 255, "pwm5": 255})
            self.assertEqual(ctl.estado, "ciego")


# --- 6: calendario («quiet» = noche) -----------------------------------------------------------

FEST = wv.leer_festivos(AQUI / "festivos-madrid.txt")


def modo(texto, festivos=FEST):
    d = wv.decidir(madrid(texto), festivos, "dia")
    return "noche" if d.perfil == "quiet" else d.perfil


class Calendario(unittest.TestCase):
    def test_lista_cubre_2026_y_2027(self):
        self.assertEqual(FEST.anios, {2026, 2027})
        for anio in (2026, 2027):
            self.assertEqual(sum(1 for f in FEST.fechas if f.year == anio), 12)

    def test_bordes(self):
        self.assertEqual(modo("2026-10-14T20:59:59"), "dia")
        self.assertEqual(modo("2026-10-14T21:00:00"), "noche")
        self.assertEqual(modo("2026-10-14T08:59:59"), "noche")
        self.assertEqual(modo("2026-10-14T09:00:00"), "dia")
        for dia in ("2026-10-17", "2026-10-18"):
            self.assertEqual(modo(f"{dia}T09:59:59"), "noche")
            self.assertEqual(modo(f"{dia}T10:00:00"), "dia")
        self.assertEqual(modo("2026-10-12T09:59:59"), "noche")   # festivo en lunes
        self.assertEqual(modo("2026-10-12T10:00"), "dia")
        self.assertEqual(modo("2026-11-02T09:30"), "noche")
        self.assertEqual(modo("2027-03-19T09:30"), "noche")
        self.assertEqual(modo("2026-03-19T09:30"), "dia")

    def test_falla_hacia_la_noche(self):
        self.assertEqual(modo("2026-10-14T09:30", festivos=None), "noche")
        self.assertEqual(modo("2028-03-01T09:30"), "noche")
        self.assertEqual(modo("2028-03-01T10:00"), "dia")
        with self.assertRaises(ValueError):
            wv.decidir(dt.datetime(2026, 10, 14, 9, 30), FEST, "dia")

    def test_utc_y_cambio_de_hora(self):
        utc = dt.timezone.utc
        self.assertEqual(wv.decidir(dt.datetime(2026, 10, 14, 6, 59, tzinfo=utc), FEST, "dia").perfil, "quiet")
        self.assertEqual(wv.decidir(dt.datetime(2026, 10, 14, 7, 0, tzinfo=utc), FEST, "dia").perfil, "dia")
        self.assertEqual(wv.decidir(dt.datetime(2026, 12, 2, 20, 0, tzinfo=utc), FEST, "dia").perfil, "quiet")
        for dia in ("2026-10-25", "2027-03-28"):
            self.assertEqual(modo(f"{dia}T09:59"), "noche")
            self.assertEqual(modo(f"{dia}T10:00"), "dia")

    def test_decidir_modo_mapea_y_falla_hacia_la_noche(self):
        self.assertEqual(wv.decidir_modo(CFG, madrid("2026-10-14T12:00"))[0], "dia")
        self.assertEqual(wv.decidir_modo(CFG, madrid("2026-10-14T22:00"))[0], "noche")
        forzado = wv.Config()
        forzado.valores["MODO"] = "noche"
        self.assertEqual(wv.decidir_modo(forzado, madrid("2026-10-14T12:00"))[0], "noche")
        viejo = wv.FESTIVOS
        try:
            wv.FESTIVOS = Path("/nonexistent/festivos.txt")
            m, _, cubiertos = wv.decidir_modo(CFG, madrid("2026-10-14T09:30"))
            self.assertEqual((m, cubiertos), ("noche", False))
            m, _, cubiertos = wv.decidir_modo(forzado, madrid("2026-10-14T12:00"))   # auditoría r1, E4
            self.assertEqual((m, cubiertos), ("noche", False))
            with tempfile.TemporaryDirectory() as d:                                  # autoreview #16687
                wv.FESTIVOS = Path(d) / "festivos-madrid.txt"
                wv.FESTIVOS.write_bytes(b"# cubre: 2026\n2026-01-01 A\xf1o Nuevo\n")   # latin-1, no UTF-8
                self.assertEqual(wv.decidir_modo(CFG, madrid("2026-10-14T09:30"))[:3:2], ("noche", False))
                self.assertEqual(wv.decidir_modo(CFG, madrid("2026-10-14T23:00"))[0], "noche")
                self.assertEqual(wv.decidir_modo(forzado, madrid("2026-10-14T12:00"))[:3:2], ("noche", False))
        finally:
            wv.FESTIVOS = viejo


class TransicionesDelCalendario(Banco):
    def test_21_00_bajan_los_dos_canales_de_a_4_y_09_00_suben_de_a_20(self):
        ctl = self.preparado()
        dia = {c: wv.curva(44, c, False, CFG) for c in wv.CANALES}        # SYS 94, SATA 144
        noche = {c: wv.curva(44, c, True, CFG) for c in wv.CANALES}       # SYS 74, SATA 118
        with self.callar():
            for _ in range(8):                                             # régimen diurno estable
                self.un_ciclo(ctl, [44] * 6, ahora=madrid("2026-10-14T20:50"))
            self.assertEqual(ctl.aplicado, dia)
            historia = [dict(ctl.aplicado)]
            for m in range(11):                                            # 21:00 ... 21:10
                self.un_ciclo(ctl, [44] * 6, ahora=madrid("2026-10-14T21:00") + dt.timedelta(minutes=m))
                historia.append(dict(ctl.aplicado))
            for c in wv.CANALES:
                h = [x[c] for x in historia]
                self.assertTrue(all(0 <= a - b <= 4 for a, b in zip(h, h[1:])), (c, h))
            self.assertEqual(ctl.aplicado, noche)                          # en la curva nocturna antes de 21:11
            historia = [dict(ctl.aplicado)]
            for m in range(5):
                self.un_ciclo(ctl, [44] * 6, ahora=madrid("2026-10-15T09:00") + dt.timedelta(minutes=m))
                historia.append(dict(ctl.aplicado))
            for c in wv.CANALES:
                h = [x[c] for x in historia]
                self.assertTrue(all(0 <= b - a <= 20 for a, b in zip(h, h[1:])), (c, h))
            self.assertEqual(ctl.aplicado, dia)


# --- 8: lecturas ---------------------------------------------------------------------------------

def smart(temp=None, estado=0, tabla=True, power=None, quitar=()):
    j = {"smartctl": {"exit_status": estado}}
    if tabla:
        j["ata_smart_attributes"] = {"table": [{"id": 194, "name": "Temperature_Celsius"}]}
    if temp is not None:
        j["temperature"] = {"current": temp}
    if power:
        j["power_mode"] = {"name": power}
    for k in quitar:
        j.pop(k, None)
    return json.dumps(j)


class Lecturas(Banco):
    def test_interpretar_smartctl(self):
        self.assertEqual(wv.interpretar_smartctl(smart(41), 0), wv.Lectura("ok", 41))
        self.assertEqual(wv.interpretar_smartctl(smart(None, 2, False, "STANDBY"), 2).tipo, "standby")
        self.assertEqual(wv.interpretar_smartctl(smart(None, 2, False), 2).tipo, "fallo")   # no abrió
        self.assertEqual(wv.interpretar_smartctl(smart(None), 0).tipo, "fallo")            # sin current
        self.assertEqual(wv.interpretar_smartctl(json.dumps({"temperature": {"current": "41"}}), 0).tipo, "fallo")
        self.assertEqual(wv.interpretar_smartctl(json.dumps({"temperature": {"current": True}}), 0).tipo, "fallo")
        self.assertEqual(wv.interpretar_smartctl("no es json", 0).tipo, "fallo")
        self.assertEqual(wv.interpretar_smartctl(smart(41, 1), 1).tipo, "fallo")           # línea de órdenes
        for bits in (4, 8, 16, 32, 64, 128, 8 | 64):                                        # salud: no invalidan
            self.assertEqual(wv.interpretar_smartctl(smart(41, bits), bits), wv.Lectura("ok", 41), bits)

    def test_verosimilitud(self):
        ed = wv.EstadoDisco()
        wv.verosimilitud(ed, ok(45))
        self.assertEqual((ed.temp, ed.base), (45, 45))
        wv.verosimilitud(ed, ok(47))                                     # 45 -> 47 aceptada
        self.assertEqual((ed.temp, ed.base, ed.sospechosa), (47, 47, False))
        ed = wv.EstadoDisco(base=45)
        for _ in range(5):                                               # 45 -> 59: sospechosa, nunca base
            wv.verosimilitud(ed, ok(59))
            self.assertEqual((ed.temp, ed.sospechosa, ed.base), (59, True, 45))
        ed = wv.EstadoDisco(base=45)
        wv.verosimilitud(ed, ok(30))
        self.assertIsNone(ed.temp)
        wv.verosimilitud(ed, ok(30))
        self.assertIsNone(ed.temp)
        wv.verosimilitud(ed, ok(30))                                     # tercera: nueva base < 50
        self.assertEqual((ed.temp, ed.base), (30, 30))
        for t in (4, 91):
            ed = wv.EstadoDisco(base=40)
            wv.verosimilitud(ed, ok(t))
            self.assertEqual((ed.temp, ed.fallos, ed.base), (None, 1, 40))

    def test_salto_sospechoso_entra_en_t_ctrl(self):
        ctl = self.preparado()
        with self.callar():
            self.un_ciclo(ctl, [45, 40, 40, 40, 40, 40])
            self.un_ciclo(ctl, [59, 40, 40, 40, 40, 40])
        self.assertEqual(ctl.t_ctrl, 59)
        self.assertTrue(ctl.discos["ata-DISCO0"].sospechosa)
        self.assertEqual(ctl.objetivo["pwm5"], 255)
        m = wv.texto_metrica(ctl, 0)
        self.assertIn('wtr_disco_lectura_sospechosa{disco="ata-DISCO0",dev="sda",modelo="MODELO X"} 1', m)

    def test_standby_excluye_y_todos_en_standby_es_piso(self):
        ctl = self.preparado()
        sb = wv.Lectura("standby")
        with self.callar():
            self.un_ciclo(ctl, [sb, sb, 50, 40, 40, 40])
            self.assertEqual(ctl.t_ctrl, 50)
            for _ in range(40):
                self.un_ciclo(ctl, [sb] * 6)
        self.assertTrue(ctl.todos_standby)
        self.assertFalse(ctl.ciego)
        self.assertEqual(ctl.objetivo, {"pwm3": 77, "pwm5": 115})
        self.assertEqual(ctl.aplicado, {"pwm3": 77, "pwm5": 115})

    def test_cinco_fallos_excluyen_y_el_sexto_marca_lectura_ok_0(self):
        ctl = self.preparado()
        f = wv.Lectura("fallo")
        with self.callar():
            self.un_ciclo(ctl, [44, 40, 40, 40, 40, 40])
            for n in range(1, 7):
                self.un_ciclo(ctl, [f, 40, 40, 40, 40, 40])
                m = wv.texto_metrica(ctl, 0)
                valor = re.search(r'wtr_disco_lectura_ok\{disco="ata-DISCO0".*\} (\d)', m).group(1)
                self.assertEqual(valor, "1" if n <= 5 else "0", n)
                self.assertEqual(ctl.t_ctrl, 40)

    def test_todos_fallando_tres_ciclos_es_ciego_y_255(self):
        ctl = self.preparado()
        f = wv.Lectura("fallo")
        with self.callar():
            self.un_ciclo(ctl, [44] * 6)
            self.un_ciclo(ctl, [f] * 6)
            self.un_ciclo(ctl, [f] * 6)
            self.assertFalse(ctl.ciego)
            self.assertNotEqual(ctl.aplicado["pwm5"], 255)
            self.un_ciclo(ctl, [f] * 6)
        self.assertTrue(ctl.ciego)
        self.assertEqual(ctl.aplicado, {"pwm3": 255, "pwm5": 255})
        self.assertEqual((self.leer("pwm3_auto_start"), self.leer("pwm5_auto_start")), (255, 255))
        self.assertNotIn("wtr_disco_temperatura_max_celsius ", wv.texto_metrica(ctl, 0))

    def test_tras_ciego_todos_en_standby_vuelve_al_piso(self):  # auditoría r1, C1/E2
        ctl = self.preparado()
        f, sb = wv.Lectura("fallo"), wv.Lectura("standby")
        with self.callar():
            self.un_ciclo(ctl, [44] * 6)
            for _ in range(4):
                self.un_ciclo(ctl, [f] * 6)
            self.assertTrue(ctl.ciego)
            for _ in range(60):
                self.un_ciclo(ctl, [sb] * 6)
        self.assertFalse(ctl.ciego)
        self.assertEqual(ctl.estado, "ok")
        self.assertEqual(ctl.objetivo, {"pwm3": 77, "pwm5": 115})
        self.assertEqual(ctl.aplicado, {"pwm3": 77, "pwm5": 115})

    def test_smartctl_falso_en_path_paralelo_y_con_plazo(self):
        d = Path(self.tmp.name) / "smart"
        d.mkdir()
        falso = d / "smartctl"
        falso.write_text(f"#!{sys.executable}\n"
                         "import json, os, sys, time\n"
                         "dev = os.path.basename(sys.argv[-1])\n"
                         f"p = os.path.join({str(d)!r}, dev + '.json')\n"
                         "c = json.load(open(p))\n"
                         "time.sleep(c.get('dormir', 0))\n"
                         "print(json.dumps(c['salida'])); sys.exit(c.get('rc', 0))\n")
        falso.chmod(falso.stat().st_mode | stat.S_IEXEC)
        (d / "sda.json").write_text(json.dumps({"salida": json.loads(smart(41))}))
        (d / "sdb.json").write_text(json.dumps({"salida": json.loads(smart(None, 2, False, "STANDBY")), "rc": 2}))
        (d / "sdc.json").write_text(json.dumps({"salida": json.loads(smart(43)), "dormir": 30}))
        wv.SMARTCTL = str(falso)
        ds = [wv.Disco("a", "sda", "/dev/sda"), wv.Disco("b", "sdb", "/dev/sdb"), wv.Disco("c", "sdc", "/dev/sdc")]
        inicio = time.monotonic()
        res = wv.leer_discos(ds, timeout=3)
        self.assertLess(time.monotonic() - inicio, 8)
        self.assertEqual(res["a"], wv.Lectura("ok", 41))
        self.assertEqual(res["b"].tipo, "standby")
        self.assertEqual(res["c"].tipo, "fallo")

    def test_descubrimiento_por_lsblk_y_by_id(self):
        d = Path(self.tmp.name)
        lsblk = d / "lsblk"
        salida = {"blockdevices": [
            {"name": "sda", "rota": True, "tran": "sata", "model": "WDC WD40PURX-64NZ6Y0"},
            {"name": "sdg", "rota": False, "tran": "usb", "model": "MassStorageClass"},
            {"name": "nvme0n1", "rota": False, "tran": "nvme", "model": "WDC PC SN520"},
            {"name": "sdf", "rota": "1", "tran": "sata", "model": "HGST HUS724040ALA640"}]}
        lsblk.write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(salida)}\nEOF\n")
        lsblk.chmod(0o755)
        byid = d / "by-id"
        byid.mkdir()
        (d / "sda").touch()
        (d / "sdf").touch()
        (byid / "ata-WDC_WD40PURX-64NZ6Y0_WD-EXAMPLE0003").symlink_to(d / "sda")
        (byid / "ata-WDC_WD40PURX-64NZ6Y0_WD-EXAMPLE0003-part1").symlink_to(d / "sda")
        wv.LSBLK, wv.BYID = str(lsblk), byid
        ds = wv.descubrir_discos(CFG)
        self.assertEqual([(x.id, x.dev, x.modelo) for x in ds],
                         [("ata-WDC_WD40PURX-64NZ6Y0_WD-EXAMPLE0003", "sda", "WDC WD40PURX-64NZ6Y0"),
                          ("sdf", "sdf", "HGST HUS724040ALA640")])
        self.assertEqual(ds[0].ruta, str(byid / "ata-WDC_WD40PURX-64NZ6Y0_WD-EXAMPLE0003"))


# --- 9: corrección de canal y respaldo -----------------------------------------------------------

class CorreccionYRespaldo(Banco):
    def test_canal_fuera_de_automatico_o_start_alterado_se_corrige(self):
        ctl = self.preparado()
        self.poner("pwm3_enable", 1)
        self.poner("pwm5_auto_point2_temp", 20000)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertIn(("pwm3_enable", 2), self.escritos)
        self.assertIn(("pwm5_auto_point2_temp", 0), self.escritos)
        self.assertGreaterEqual(ctl.correcciones, 2)

    def test_auto_start_ajeno_se_registra_y_se_sigue_desde_lo_leido(self):
        ctl = self.preparado()
        self.poner("pwm5_auto_start", 200)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertEqual(ctl.aplicado["pwm5"], 200)       # objetivo 124 < 200 - 6: espera 3 ciclos
        self.assertEqual(ctl.correcciones, 1)

    def test_respaldo_153_77_y_enable_2(self):
        self.poner("pwm3_auto_start", 255)
        self.poner("pwm5_auto_start", 200)
        self.poner("pwm5_enable", 1)
        with self.callar():
            self.assertEqual(wv.main(["respaldo"]), 0)
        self.assertEqual((self.leer("pwm3_auto_start"), self.leer("pwm5_auto_start"), self.leer("pwm5_enable")),
                         (77, 153, 2))

    def test_respaldo_sin_sysfs_sale_0_sin_escribir(self):
        wv.HWMON_GLOB = str(Path(self.tmp.name) / "nada" / "hwmon*")
        with self.callar():
            self.assertEqual(wv.main(["respaldo"]), 0)
        self.assertEqual(self.escritos, [])

    def test_simular_no_escribe(self):
        ctl = self.control(simular=True)
        with self.callar():
            wv.preparar(ctl, self.hwmon(simular=True))
            for t in (40, 46, 52, 44):
                self.un_ciclo(ctl, [t] * 6, simular=True)
            self.assertEqual(wv.main(["respaldo", "--simular"]), 0)
        self.assertEqual(self.escritos, [])
        self.assertEqual(ctl.estado, "simulando")
        self.assertEqual(ctl.leido, {"pwm3": 100, "pwm5": 100})


# --- 10 y 11: ventilador parado y escalón huérfano ------------------------------------------------

class VentiladorParadoYEscalon(Banco):
    def test_fan5_parado_tres_ciclos_pone_pwm3_en_255(self):
        ctl = self.preparado()
        self.poner("fan5_input", 0)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
            self.un_ciclo(ctl, [40] * 6)
            self.assertEqual(ctl.aplicado["pwm3"], wv.curva(40, "pwm3", False, CFG))   # 83, aún no 255
            self.un_ciclo(ctl, [40] * 6)
        self.assertEqual(ctl.estado, "ventilador_parado")
        self.assertEqual(ctl.aplicado["pwm3"], 255)
        self.assertEqual(ctl.objetivo_dia["pwm3"], 255)
        self.assertEqual(self.leer("pwm3_auto_start"), 255)
        self.assertLess(ctl.aplicado["pwm5"], 255)
        self.poner("fan5_input", 1190)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertEqual(ctl.estado, "ok")

    def test_de_noche_cuenta_con_el_piso_nocturno(self):  # enmienda rev. 5.1 §4.3
        ctl = self.preparado()
        with self.callar():
            for _ in range(10):
                self.un_ciclo(ctl, [36] * 6, ahora=NOCHE)
            self.assertEqual(ctl.aplicado["pwm3"], 64)          # bajo el piso diurno (77)
            self.poner("fan3_input", 0)
            for _ in range(3):
                self.un_ciclo(ctl, [36] * 6, ahora=NOCHE)
        self.assertEqual(ctl.estado, "ventilador_parado")
        self.assertEqual(ctl.aplicado["pwm5"], 255)

    def test_escalon_huerfano_se_ignora_y_el_vigente_da_255_sin_escribir(self):
        ctl = self.preparado()
        self.escalon(120)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertFalse(ctl.escalon)
        self.escalon(10)
        self.poner("pwm3_auto_start", 255)
        self.poner("pwm5_auto_start", 255)
        self.escritos.clear()
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        self.assertTrue(ctl.escalon)
        self.assertEqual(ctl.objetivo, {"pwm3": 255, "pwm5": 255})
        self.assertEqual(ctl.objetivo_dia, {"pwm3": 255, "pwm5": 255})
        self.assertEqual(self.escritos, [])
        self.assertIn("wtr_ventiladores_escalon_guardia_vigente 1", wv.texto_metrica(ctl, 0))
        wv.ESCALON.unlink()                                  # la guardia lo retira: bajada en rampa
        with self.callar():
            for _ in range(3):
                self.un_ciclo(ctl, [40] * 6)
        self.assertEqual(ctl.aplicado["pwm5"], 251)
        self.assertEqual(ctl.correcciones, 0)


# --- 12: calibrar --------------------------------------------------------------------------------

class Calibrar(Banco):
    def systemctl(self, rc):
        p = Path(self.tmp.name) / f"systemctl-{rc}"
        p.write_text(f"#!/bin/sh\nexit {rc}\n")
        p.chmod(0o755)
        wv.SYSTEMCTL = str(p)

    def test_con_el_servicio_activo_se_niega(self):
        self.systemctl(0)
        with self.callar(), redirect_stderr(io.StringIO()):
            self.assertEqual(wv.main(["calibrar", "--canal", "pwm5", "--pwm", "128", "--minutos", "0"]), 1)
        self.assertEqual(self.escritos, [])

    def test_con_el_lock_tomado_se_niega(self):
        self.systemctl(3)
        lock = wv.tomar_lock()
        try:
            with self.callar(), redirect_stderr(io.StringIO()):
                self.assertEqual(wv.main(["calibrar", "--canal", "pwm3", "--pwm", "64", "--minutos", "0"]), 1)
        finally:
            lock.close()
        self.assertEqual(self.escritos, [])

    def test_pwm_fuera_de_rango_se_niega(self):
        self.systemctl(3)
        with self.callar(), redirect_stderr(io.StringIO()):
            self.assertEqual(wv.main(["calibrar", "--canal", "pwm3", "--pwm", "40", "--minutos", "0"]), 2)
        self.assertEqual(self.escritos, [])

    def test_si_la_guardia_escala_a_mitad_no_le_baja_el_canal(self):  # auditoría r1, C5
        from unittest import mock
        self.systemctl(3)
        wv.LSBLK = "/nonexistent/lsblk"
        real = time.sleep

        def dormir(_s):
            self.escalon(0)
            self.poner("pwm5_auto_start", 255)          # la guardia dio todo el aire
            real(0.05)
        with mock.patch.object(wv.time, "sleep", dormir), self.callar(), redirect_stderr(io.StringIO()):
            self.assertEqual(wv.main(["calibrar", "--canal", "pwm5", "--pwm", "128", "--minutos", "0.002"]), 0)
        self.assertEqual(self.escritos, [("pwm5_auto_start", 128)])
        self.assertEqual(self.leer("pwm5_auto_start"), 255)

    def test_libre_escribe_y_vuelve_al_respaldo(self):
        self.systemctl(3)
        wv.LSBLK = "/nonexistent/lsblk"
        with self.callar():
            self.assertEqual(wv.main(["calibrar", "--canal", "pwm5", "--pwm", "128", "--minutos", "0"]), 0)
        self.assertEqual(self.escritos, [("pwm5_auto_start", 128), ("pwm5_auto_start", 153)])


# --- 17: métrica ---------------------------------------------------------------------------------

LINEA = re.compile(r'^(# (HELP|TYPE) \w+ .+|[a-z_]+(\{([a-z_]+="[^"\\]*",?)*\})? -?\d+(\.\d+)?)$')


class Metrica(Banco):
    def comprobar(self, ctl):
        txt = wv.texto_metrica(ctl, 1791900000)
        self.assertTrue(txt.isascii())
        for linea in txt.splitlines():
            self.assertRegex(linea, LINEA)
        for e in wv.ESTADOS:
            self.assertIn(f'wtr_ventiladores_estado{{estado="{e}"}} {int(ctl.estado == e)}', txt)
        for m in ("dia", "noche"):
            self.assertIn(f'wtr_ventiladores_modo{{modo="{m}"}}', txt)
        for s in ("sobre_noche", "escalon_guardia_vigente", "correcciones_total", "festivos_cubiertos",
                  "config_ok", "ultimo_ciclo_segundos"):
            self.assertIn(f"wtr_ventiladores_{s} ", txt)
        for s in ("objetivo", "objetivo_dia", "aplicado", "leido"):     # familia presente aun sin muestras
            self.assertIn(f"# TYPE wtr_ventiladores_pwm_{s} gauge", txt)
        return txt

    def test_formato_y_todas_las_series_en_todos_los_estados(self):
        ctl = self.preparado()
        with self.callar():
            self.un_ciclo(ctl, [41, 40, 39, 38, 37, 36])
        txt = self.comprobar(ctl)
        self.assertIn('wtr_ventiladores_pwm_aplicado{canal="pwm5",ventilador="sata"}', txt)
        self.assertIn('wtr_ventiladores_pwm_leido{canal="pwm3",ventilador="sys"}', txt)
        self.assertIn('wtr_ventiladores_pwm_objetivo_dia{canal="pwm5",ventilador="sata"}', txt)
        self.assertIn("wtr_disco_temperatura_max_celsius 41", txt)
        with self.callar():
            for _ in range(3):
                self.un_ciclo(ctl, [wv.Lectura("fallo")] * 6)
        self.assertEqual(ctl.estado, "ciego")
        txt = self.comprobar(ctl)
        self.assertIn('wtr_ventiladores_pwm_objetivo_dia{canal="pwm3",ventilador="sys"} 255', txt)
        sin = self.control()
        sin.estado = "sin_driver"
        self.comprobar(sin)

    def test_objetivo_dia_es_la_curva_diurna(self):  # D14
        ctl = self.preparado()
        with self.callar():
            self.un_ciclo(ctl, [42] * 6, ahora=NOCHE)
        self.assertEqual(ctl.objetivo, {"pwm3": 71, "pwm5": 109})
        self.assertEqual(ctl.objetivo_dia, {"pwm3": 89, "pwm5": 134})
        txt = wv.texto_metrica(ctl, 0)
        self.assertIn('wtr_ventiladores_pwm_objetivo_dia{canal="pwm5",ventilador="sata"} 134', txt)
        self.assertIn('wtr_ventiladores_pwm_objetivo_dia{canal="pwm3",ventilador="sys"} 89', txt)
        self.assertIn('wtr_ventiladores_pwm_objetivo{canal="pwm5",ventilador="sata"} 109', txt)
        self.assertIn('wtr_ventiladores_pwm_objetivo{canal="pwm3",ventilador="sys"} 71', txt)
        self.assertEqual(wv._resumen(ctl),
                         "T=42 modo=noche sys obj=71 dia=89 apl=77 sata obj=109 dia=134 apl=153 estado=ok")

    def test_config_invalida_no_degrada_el_estado(self):  # D16
        wv.CONF.write_text("NOCHE_SATA=153\n", encoding="utf-8")       # noche = día: incoherente
        cfg = wv.leer_config(wv.CONF)
        self.assertFalse(cfg.ok)
        ctl = self.preparado(cfg=cfg)
        with self.callar():
            self.un_ciclo(ctl, [40] * 6)
        txt = self.comprobar(ctl)
        self.assertIn("wtr_ventiladores_config_ok 0", txt)
        self.assertIn('wtr_ventiladores_estado{estado="ok"} 1', txt)

    def test_sobre_noche(self):
        ctl = self.preparado()
        with self.callar():
            for _ in range(5):
                self.un_ciclo(ctl, [48] * 6, ahora=NOCHE)
        self.assertIn("wtr_ventiladores_sobre_noche 1", wv.texto_metrica(ctl, 0))
        self.assertIn('wtr_ventiladores_modo{modo="noche"} 1', wv.texto_metrica(ctl, 0))

    def test_escritura_atomica_0644(self):
        ctl = self.preparado()
        wv.publicar(ctl)
        self.assertEqual(oct(wv.METRICA.stat().st_mode & 0o777), "0o644")
        self.assertEqual([p.name for p in wv.METRICA.parent.iterdir()], ["wtr_ventiladores.prom"])


class Configuracion(unittest.TestCase):
    def cfg(self, texto):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text(texto, encoding="utf-8")
            return wv.leer_config(p)

    def curva_de_fabrica(self, c):
        self.assertEqual({k: c[k] for k in wv.CURVA}, {k: wv.DEFECTO[k] for k in wv.CURVA})

    def test_el_conf_del_repo_es_valido_y_coincide_con_fabrica(self):
        c = wv.leer_config(AQUI / "wtr-ventiladores.conf")
        self.assertTrue(c.ok, c.errores)
        self.assertEqual(c.valores, wv.DEFECTO)
        texto = (AQUI / "wtr-ventiladores.conf").read_text(encoding="utf-8")
        claves = set(re.findall(r"^([A-Z_]+)=", texto, re.MULTILINE))
        self.assertEqual(claves, set(wv.DEFECTO))                        # las claves nuevas, explícitas

    def test_invalidos_usan_fabrica(self):
        c = self.cfg("MODO=turbo\nPISO_SYS=40\nT_MAX_C=abc\nOTRA=1\nsin igual\nDISCOS=/etc/passwd\n")
        self.assertFalse(c.ok)
        self.assertEqual(len(c.errores), 6)
        self.assertEqual((c["MODO"], c["PISO_SYS"], c["T_MAX_C"], c["DISCOS"]), ("auto", 77, 52, ""))

    def test_curva_incoherente_vuelve_entera_a_fabrica(self):
        c = self.cfg("T_SILENCIO_C=37\nNOCHE_SYS=120\n")
        self.assertFalse(c.ok)
        self.assertEqual((c["T_SILENCIO_C"], c["NOCHE_SYS"]), (46, 77))
        for texto in ("NOCHE_SATA=153\nPISO_SATA=120\n",           # NOCHE = SILENCIO: noche = día
                      "PISO_NOCHE_SYS=77\nNOCHE_SYS=90\n",         # PISO_NOCHE = PISO
                      "PISO_NOCHE_SATA=120\nNOCHE_SATA=140\n"):    # PISO_NOCHE > PISO
            c = self.cfg(texto)
            self.assertFalse(c.ok, texto)
            self.assertEqual(len(c.errores), 1, (texto, c.errores))
            self.assertIn("MODO=dia", c.errores[0])
            self.curva_de_fabrica(c)
        c = self.cfg("CICLOS_BAJADA=5\nCICLOS_CONVERGENCIA=2\nNOCHE_SATA=120\n")   # sólo los ciclos vuelven
        self.assertFalse(c.ok)
        self.assertEqual((c["CICLOS_BAJADA"], c["CICLOS_CONVERGENCIA"], c["NOCHE_SATA"]), (3, 10, 120))
        c = self.cfg("PISO_NOCHE_SYS=50\nNOCHE_SATA=120\n")        # fuera de rango: error de valor, no de curva
        self.assertFalse(c.ok)
        self.assertEqual(len(c.errores), 1, c.errores)
        self.assertIn("PISO_NOCHE_SYS='50'", c.errores[0])
        self.assertEqual((c["PISO_NOCHE_SYS"], c["NOCHE_SATA"]), (64, 120))

    def test_conf_de_la_revision_4_cae_a_fabrica_con_config_ok_0(self):
        self.assertEqual(hashlib.sha256(CONF_REV4.encode("utf-8")).hexdigest(),   # CONF_PREVIAS de instalar.sh
                         "27dfc55c2479adf916aee5a1eff817a307500a2716f9a55445ca2fdbe5da5cc3")
        c = self.cfg(CONF_REV4)
        self.assertFalse(c.ok)
        self.assertEqual(len(c.errores), 1, c.errores)        # NOCHE_SATA=153 = SILENCIO_SATA (invariante 3)
        self.assertIn("NOCHE_SATA < SILENCIO_SATA", c.errores[0])
        self.assertIn("MODO=dia", c.errores[0])
        self.curva_de_fabrica(c)

    def test_claves_nuevas_ausentes_usan_fabrica(self):
        c = self.cfg("NOCHE_SATA=120\nNOCHE_SYS=70\nCICLOS_BAJADA=3\n")
        self.assertTrue(c.ok, c.errores)
        self.assertEqual((c["PISO_NOCHE_SATA"], c["PISO_NOCHE_SYS"], c["CICLOS_CONVERGENCIA"]), (89, 64, 10))
        self.assertEqual((c["NOCHE_SATA"], c["NOCHE_SYS"]), (120, 70))

    def test_valores_de_h8(self):
        c = self.cfg("MODO=noche\nT_SILENCIO_C=39\nT_CONVERGE_C=43\n")
        self.assertTrue(c.ok, c.errores)
        self.assertEqual(c.discos, [])


if __name__ == "__main__":
    unittest.main()
