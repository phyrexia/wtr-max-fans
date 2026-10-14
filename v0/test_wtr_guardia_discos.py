# SPDX-FileCopyrightText: 2026 Gabriel González Pineda <https://github.com/phyrexia>
# SPDX-License-Identifier: Apache-2.0
"""Pruebas de wtr-guardia-discos (spec 2026-10-13 §8.1, casos 13 a 16 y la parte de la guardia del 17).
`shutdown` y `systemctl` falsos que registran, sysrq en un archivo, sysfs simulado con y sin
pwm5_auto_start, /proc/stat con btime controlable.

Correr desde la raíz del repositorio (no tocan el hardware: sysfs, smartctl y systemd son dobles):
  python3 -m unittest discover -s v0 -p 'test_*.py' -v
"""

import importlib.machinery
import importlib.util
import io
import json
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

AQUI = Path(__file__).resolve().parent
_cargador = importlib.machinery.SourceFileLoader("wtr_guardia_discos", str(AQUI / "wtr-guardia-discos"))
_spec = importlib.util.spec_from_loader("wtr_guardia_discos", _cargador)
gd = importlib.util.module_from_spec(_spec)
sys.modules["wtr_guardia_discos"] = gd
_cargador.exec_module(gd)

HGST = "ata-HGST_HUS724040ALA640_EXAMPLE0001"
OTROS = [f"ata-DISCO{i}" for i in range(1, 6)]
DISCOS = [gd.Disco(HGST, "sdf", "/dev/sdf")] + [gd.Disco(i, f"sd{chr(96 + n)}", f"/dev/sd{chr(96 + n)}")
                                                   for n, i in enumerate(OTROS, 1)]
CICLO = 30


def lect(hgst, *otros):
    """Lecturas del HGST y de los otros cinco (si se dan menos, el resto a 40 °C)."""
    otros = list(otros) + [40] * (5 - len(otros))
    res = {}
    for ident, t in zip([HGST] + OTROS, [hgst] + otros):
        res[ident] = t if isinstance(t, gd.Lectura) else gd.Lectura("ok", t)
    return res


class Banco(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        raiz = Path(self.tmp.name)
        self.hw = raiz / "sys" / "hwmon6"
        self.hw.mkdir(parents=True)
        (self.hw / "name").write_text("it8613\n")
        for c, start in (("pwm3", 77), ("pwm5", 153)):
            (self.hw / f"{c}_enable").write_text("2\n")
            (self.hw / f"{c}_auto_start").write_text(f"{start}\n")
        self.log = raiz / "ordenes.log"
        self.viejos = {k: getattr(gd, k) for k in ("HWMON_GLOB", "METRICA", "RUN", "ESTADO_DIR", "SHUTDOWN",
                                                    "SYSTEMCTL", "SYSRQ", "PROC_STAT", "SYSRQ_PAUSA_S",
                                                    "_escribir_sysfs")}
        gd.HWMON_GLOB = str(raiz / "sys" / "hwmon*")
        gd.METRICA = raiz / "textfile" / "wtr_guardia_discos.prom"
        gd.RUN = raiz / "run"
        gd.ESTADO_DIR = raiz / "estado"
        gd.SYSRQ = raiz / "sysrq-trigger"
        gd.PROC_STAT = raiz / "stat"
        gd.SYSRQ_PAUSA_S = 0
        self.btime(time.time() - 86400)
        self.ordenes(shutdown=0, systemctl=0)
        self.escritos = []
        original = self.viejos["_escribir_sysfs"]

        def kernel(p, valor):
            self.escritos.append((p.name, int(valor)))
            original(p, valor)
            if p.name.endswith("_enable") and int(valor) == 0:       # enable=0 pone base+3 en 255
                c = p.name.split("_")[0]
                if (p.parent / f"{c}_auto_start").exists():
                    (p.parent / f"{c}_auto_start").write_text("255\n")
        gd._escribir_sysfs = kernel
        self.g = gd.Guardia()
        self.cfg = gd.Config()
        self.ahora = 1_791_900_000.0

    def tearDown(self):
        for k, v in self.viejos.items():
            setattr(gd, k, v)
        self.tmp.cleanup()

    def btime(self, t):
        gd.PROC_STAT.write_text(f"cpu  1 2 3\nbtime {int(t)}\nprocesses 10\n")

    def ordenes(self, shutdown, systemctl):
        """shutdown y systemctl falsos: registran argv y, en ese instante, si existen el marcador y
        la métrica (para comprobar el orden), y salen con el código pedido."""
        for nombre, rc in (("shutdown", shutdown), ("systemctl", systemctl)):
            p = Path(self.tmp.name) / nombre
            p.write_text("#!/bin/sh\n"
                         f'm=no; [ -f "{gd.ESTADO_DIR}/ultimo-apagado" ] && m=si\n'
                         f'p=no; grep -q "^wtr_guardia_discos_apagado_pendiente 1" "{gd.METRICA}" 2>/dev/null && p=si\n'
                         f'echo "{nombre} $* marcador=$m metrica=$p" >> "{self.log}"\n'
                         f"exit {rc}\n")
            p.chmod(0o755)
            setattr(gd, nombre.upper(), str(p))

    def registro(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def ciclo(self, lecturas, n=1):
        with redirect_stdout(io.StringIO()):
            for _ in range(n):
                gd.un_ciclo(self.g, self.cfg, self.ahora, DISCOS, lecturas)
                self.ahora += CICLO
        return self.g

    def leer(self, attr):
        return int((self.hw / attr).read_text().strip())

    def metrica(self):
        return gd.METRICA.read_text()


# --- 13: C1 y N-I4 -------------------------------------------------------------------------------

class Corroboracion(Banco):
    def test_hgst_salta_a_59_y_se_queda_aislado_sin_apagar_en_10_minutos(self):
        estados = [self.ciclo(lect(42)).estado]
        for _ in range(20):                                  # 10 min a 30 s
            estados.append(self.ciclo(lect(59)).estado)
        self.assertEqual(estados[:5], [0, 1, 1, 3, 3])       # aire al saltar, aislado a la 3.a lectura
        self.assertTrue(all(e == 3 for e in estados[3:]))
        self.assertEqual(self.registro(), [])
        self.assertTrue((gd.RUN / "escalon").exists())
        self.assertEqual((self.leer("pwm3_auto_start"), self.leer("pwm5_auto_start")), (255, 255))
        self.assertFalse((gd.ESTADO_DIR / "ultimo-apagado").exists())

    def test_hgst_sube_progresivo_a_58_con_segundo_a_51_apaga_a_la_tercera(self):
        for t in (48, 52, 56):
            self.ciclo(lect(t, 51))
        self.ciclo(lect(58, 51))
        self.ciclo(lect(58, 51))
        self.assertEqual(self.registro(), [])
        self.ciclo(lect(58, 51))
        self.assertEqual(self.g.estado, 2)
        reg = self.registro()
        self.assertEqual(len(reg), 1)
        self.assertTrue(reg[0].startswith("shutdown -h +1 wtr-guardia-discos: " + HGST + " a 58 °C, segundo 51 °C"))
        m = json.loads((gd.ESTADO_DIR / "ultimo-apagado").read_text())
        self.assertEqual((m["disco"], m["t1"], m["t2"], m["simulacro"]), (HGST, 58, 51, False))

    def test_corroboracion_exige_dos_de_tres(self):
        self.ciclo(lect(58, 51))
        self.ciclo(lect(58, 45))
        self.ciclo(lect(58, 45))
        self.assertEqual(self.g.estado, 3)                   # sólo 1 de 3 corroboradas: aislado
        self.ciclo(lect(58, 50))
        self.assertEqual(self.g.estado, 3)                   # ventana (45, 45, 50): 1 de 3
        self.ciclo(lect(58, 50))
        self.assertEqual(self.g.estado, 2)                   # (45, 50, 50): 2 de 3
        self.assertEqual(len(self.registro()), 1)

    def test_no_repite_el_apagado_antes_de_5_minutos(self):
        self.ciclo(lect(58, 51), n=3)
        self.ciclo(lect(58, 51), n=9)                        # 4,5 min más
        self.assertEqual(len(self.registro()), 1)
        self.ciclo(lect(58, 51), n=2)
        self.assertEqual(len(self.registro()), 2)

    def test_cautela_58_50_no_apaga_y_58_50_50_si(self):
        reales = gd.ESTADO_DIR
        reales.mkdir(parents=True)
        (reales / "ultimo-apagado").write_text(json.dumps({"hora": self.ahora - 600, "disco": HGST,
                                                            "simulacro": False}))
        self.btime(self.ahora - 300)
        self.ciclo(lect(gd.Lectura("fallo"), 50))            # sin base del HGST: su 58 no será un salto
        self.assertEqual(self.g.estado, 5)
        self.ciclo(lect(58, 50), n=4)
        self.assertEqual(self.g.estado, 5)
        self.assertTrue(self.g.aire)
        self.assertEqual(self.registro(), [])
        self.ciclo(lect(58, 50, 50))
        self.assertEqual(self.g.estado, 2)
        self.assertEqual(len(self.registro()), 1)

    def test_la_cautela_no_vuelve_en_un_reinicio_normal_posterior(self):
        gd.ESTADO_DIR.mkdir(parents=True)
        (gd.ESTADO_DIR / "ultimo-apagado.reconocido-20261013T100000").write_text(
            json.dumps({"hora": self.ahora - 7200, "simulacro": False}))
        self.btime(self.ahora - 3600)                        # primer arranque tras el apagado
        self.assertFalse(gd.en_cautela(self.cfg, self.ahora))  # ya pasaron más de 30 min
        self.assertTrue(gd.en_cautela(self.cfg, self.ahora - 2400))
        self.btime(self.ahora - 60)                          # otro arranque, normal
        self.assertFalse(gd.en_cautela(self.cfg, self.ahora))

    def test_sospechosas_nunca_apagan(self):
        self.ciclo(lect(42, 40))
        self.ciclo(lect(59, 55), n=10)                       # los dos saltan: sospechosos
        self.assertEqual(self.g.estado, 3)
        self.assertEqual(self.registro(), [])

    def test_simulacro_registra_sin_ejecutar(self):
        self.cfg = gd.leer_config(Path("/nonexistent"))
        self.cfg.valores["ACCION"] = "simulacro"
        self.ciclo(lect(58, 51), n=3)
        self.assertEqual(self.g.estado, 2)
        self.assertEqual(self.registro(), [])
        self.assertTrue(json.loads((gd.ESTADO_DIR / "ultimo-apagado").read_text())["simulacro"])
        self.assertIn("wtr_guardia_discos_apagado_pendiente 1", self.metrica())
        self.assertIn("wtr_guardia_discos_simulacro 1", self.metrica())
        self.assertIn("wtr_guardia_discos_apagados_total 0", self.metrica())


# --- 14: sin parche ------------------------------------------------------------------------------

class SinParche(Banco):
    def setUp(self):
        super().setUp()
        (self.hw / "pwm5_auto_start").unlink()

    def test_escalon_escribe_pwm3_255_y_pwm5_enable_0_que_se_queda(self):
        self.ciclo(lect(55))
        self.assertIn(("pwm3_auto_start", 255), self.escritos)
        self.assertIn(("pwm5_enable", 0), self.escritos)
        self.assertNotIn(("pwm5_enable", 2), self.escritos)
        m = self.metrica()
        self.assertIn('wtr_guardia_discos_manual_emergencia{canal="pwm5"} 1', m)
        self.assertIn('wtr_guardia_discos_canal_escribible{canal="pwm5"} 0', m)
        self.assertIn('wtr_guardia_discos_canal_escribible{canal="pwm3"} 1', m)
        self.ciclo(lect(45), n=3)                            # se retira bajo 50
        self.assertFalse(self.g.aire)
        self.assertFalse((gd.RUN / "escalon").exists())
        self.assertEqual(self.leer("pwm5_enable"), 0)
        self.assertNotIn(("pwm5_enable", 2), self.escritos)
        self.assertIn('wtr_guardia_discos_manual_emergencia{canal="pwm5"} 1', self.metrica())

    def test_reconocer_sin_auto_start_deja_1_y_con_auto_start_pone_0(self):
        self.ciclo(lect(55))
        with redirect_stdout(io.StringIO()) as salida:
            gd.main(["reconocer"])
        self.assertIn("sigue sin pwm5_auto_start", salida.getvalue())
        self.ciclo(lect(45))
        self.assertIn('wtr_guardia_discos_manual_emergencia{canal="pwm5"} 1', self.metrica())
        (self.hw / "pwm5_auto_start").write_text("255\n")      # DKMS reconstruido con el parche
        with redirect_stdout(io.StringIO()):
            gd.main(["reconocer"])
        self.ciclo(lect(45))
        self.assertIn('wtr_guardia_discos_manual_emergencia{canal="pwm5"} 0', self.metrica())

    def test_sin_sysfs_ninguna_excepcion_escapa_y_el_ciclo_sigue(self):
        (self.hw / "name").write_text("otro\n")
        self.ciclo(lect(56), n=3)
        self.assertEqual(self.g.estado, 1)
        self.assertIn('wtr_guardia_discos_canal_escribible{canal="pwm3"} 0', self.metrica())

    def test_escritura_que_falla_no_mata_la_guardia(self):
        def roto(p, valor):
            raise OSError(5, "Input/output error")
        gd._escribir_sysfs = roto
        self.ciclo(lect(56), n=2)
        self.assertGreaterEqual(self.g.errores, 2)
        self.assertEqual(self.g.estado, 1)
        self.assertTrue((gd.RUN / "escalon").exists())


# --- 15: acciones fallidas -----------------------------------------------------------------------

class AccionesFallidas(Banco):
    def test_cadena_shutdown_systemctl_sysrq_con_marcador_y_metrica_antes(self):
        self.ordenes(shutdown=1, systemctl=1)
        self.ciclo(lect(58, 51), n=3)
        reg = self.registro()
        self.assertEqual(len(reg), 2)
        self.assertTrue(reg[0].startswith("shutdown -h +1 "))
        self.assertTrue(reg[0].endswith("marcador=si metrica=si"))
        self.assertTrue(reg[1].startswith("systemctl poweroff"))
        self.assertEqual(gd.SYSRQ.read_text(), "o")

    def test_shutdown_inexistente_sigue_con_systemctl(self):
        gd.SHUTDOWN = "/nonexistent/shutdown"
        self.ciclo(lect(58, 51), n=3)
        self.assertEqual([r.split()[0] for r in self.registro()], ["systemctl"])
        self.assertGreaterEqual(self.g.errores, 1)

    def test_marcador_imposible_no_detiene_el_apagado(self):
        gd.ESTADO_DIR.parent.joinpath("estado").write_text("no soy un directorio")
        self.ciclo(lect(58, 51), n=3)
        self.assertEqual(len(self.registro()), 1)
        self.assertTrue(self.g.marcador_fallido)


# --- 16: ciega y reconocer -----------------------------------------------------------------------

class CiegaYReconocer(Banco):
    def test_ciega_cinco_minutos_da_aire_sin_apagar(self):
        f = gd.Lectura("fallo")
        self.ciclo(lect(40))
        self.ciclo({i: f for i in [HGST] + OTROS}, n=9)
        self.assertEqual(self.g.estado, 0)
        self.ciclo({i: f for i in [HGST] + OTROS})
        self.assertEqual(self.g.estado, 4)
        self.assertTrue(self.g.aire)
        self.assertEqual((self.leer("pwm3_auto_start"), self.leer("pwm5_auto_start")), (255, 255))
        self.assertEqual(self.registro(), [])
        self.assertNotIn("wtr_guardia_discos_temperatura_max_celsius ", self.metrica())

    def test_tras_ciega_todos_en_standby_vuelve_a_vigilar_sin_aire(self):  # auditoría r1, C1/E2
        f, sb = gd.Lectura("fallo"), gd.Lectura("standby")
        self.ciclo(lect(40))
        self.ciclo({i: f for i in [HGST] + OTROS}, n=10)
        self.assertEqual(self.g.estado, 4)
        self.ciclo({i: sb for i in [HGST] + OTROS}, n=3)
        self.assertEqual(self.g.estado, 0)
        self.assertFalse(self.g.aire)
        self.assertFalse((gd.RUN / "escalon").exists())

    def test_todos_en_standby_no_es_ciega(self):
        sb = gd.Lectura("standby")
        self.ciclo({i: sb for i in [HGST] + OTROS}, n=20)
        self.assertEqual(self.g.estado, 0)
        self.assertFalse(self.g.aire)

    def test_reconocer_baja_apagado_pendiente_y_no_toca_el_estado(self):
        f = gd.Lectura("fallo")
        gd.ESTADO_DIR.mkdir(parents=True)
        (gd.ESTADO_DIR / "ultimo-apagado").write_text(json.dumps({"hora": self.ahora - 86400 * 2,
                                                                  "simulacro": False}))
        self.ciclo({i: f for i in [HGST] + OTROS}, n=10)
        self.assertEqual(self.g.estado, 4)
        self.assertIn("wtr_guardia_discos_apagado_pendiente 1", self.metrica())
        self.assertIn("wtr_guardia_discos_apagados_total 1", self.metrica())
        with redirect_stdout(io.StringIO()):
            gd.main(["reconocer"])
        self.ciclo({i: f for i in [HGST] + OTROS})
        self.assertEqual(self.g.estado, 4)
        self.assertIn("wtr_guardia_discos_apagado_pendiente 0", self.metrica())
        self.assertIn("wtr_guardia_discos_apagados_total 1", self.metrica())
        self.assertEqual(len(list(gd.ESTADO_DIR.glob("ultimo-apagado.reconocido-*"))), 1)


# --- escalón 1 y métrica -------------------------------------------------------------------------

LINEA = re.compile(r'^(# (HELP|TYPE) \w+ .+|[a-z_]+(\{([a-z_]+="[^"\\]*",?)*\})? -?\d+(\.\d+)?)$')


class EscalonYMetrica(Banco):
    def test_escalon_refrescado_cada_ciclo_y_retirado_bajo_50(self):
        self.ciclo(lect(54))
        archivo = gd.RUN / "escalon"
        primera = archivo.stat().st_mtime_ns
        time.sleep(0.02)
        self.ciclo(lect(52))                                  # entre 50 y 54 sigue (histéresis)
        self.assertTrue(self.g.aire)
        self.assertGreater(archivo.stat().st_mtime_ns, primera)
        self.ciclo(lect(49))
        self.assertFalse(archivo.exists())
        self.assertEqual(self.g.estado, 0)

    def test_el_escalon_existe_antes_de_escribir_255(self):  # auditoría r1, C6
        vistos = []
        original = gd._escribir_sysfs

        def mirar(p, valor):
            vistos.append((p.name, int(valor), (gd.RUN / "escalon").exists()))
            original(p, valor)
        gd._escribir_sysfs = mirar
        self.ciclo(lect(55))
        self.assertIn(("pwm5_auto_start", 255, True), vistos)
        self.assertTrue(all(existe for _, _, existe in vistos), vistos)

    def test_estados_en_la_metrica_y_formato(self):
        self.ciclo(lect(44, 43))
        m = self.metrica()
        for linea in m.splitlines():
            self.assertRegex(linea, LINEA)
        self.assertTrue(m.isascii())
        for s in ("estado 0", "temperatura_max_celsius 44", "segunda_celsius 43", "apagado_pendiente 0",
                  "apagados_total 0", "ultimo_apagado_segundos 0", "simulacro 0", "errores_total 0"):
            self.assertIn(f"wtr_guardia_discos_{s}\n", m)
        for c in ("pwm3", "pwm5"):
            self.assertIn(f'wtr_guardia_discos_canal_escribible{{canal="{c}"}} 1', m)
            self.assertIn(f'wtr_guardia_discos_manual_emergencia{{canal="{c}"}} 0', m)

    def test_config(self):
        c = gd.leer_config(AQUI / "wtr-guardia-discos.conf")
        self.assertTrue(c.ok, c.errores)
        self.assertEqual(c.valores, gd.DEFECTO)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.conf"
            p.write_text("ACCION=borrar\nESCALON_C=40\n")      # incoherente con RETIRO_C=50
            c = gd.leer_config(p)
            self.assertFalse(c.ok)
            self.assertEqual((c["ACCION"], c["ESCALON_C"], c["RETIRO_C"]), ("apagar", 54, 50))
            p.write_text("ACCION=simulacro\nESCALON_C=40\nRETIRO_C=38\n")   # H9
            c = gd.leer_config(p)
            self.assertTrue(c.ok, c.errores)
            self.assertEqual((c["ACCION"], c["ESCALON_C"]), ("simulacro", 40))


if __name__ == "__main__":
    unittest.main()
