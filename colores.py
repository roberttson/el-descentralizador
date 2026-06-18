"""
Helpers de color ANSI para la consola.

Activa el modo VT en Windows automáticamente. Si la salida no es una
terminal interactiva (por ejemplo, redirigida a archivo) los códigos
quedan vacíos y el texto sale plano.
"""

import os
import sys

if os.name == "nt":
    # Idempotente: enciende el procesamiento de secuencias ANSI en cmd.exe
    os.system("")

_ACTIVO = sys.stdout.isatty()


def _seq(codigo):
    return f"\033[{codigo}m" if _ACTIVO else ""


RESET    = _seq("0")
BOLD     = _seq("1")
DIM      = _seq("2")
ITALICO  = _seq("3")

ROJO     = _seq("38;5;203")    # rojo de prensa
AMARILLO = _seq("38;5;221")    # titulares
VERDE    = _seq("38;5;114")
CYAN     = _seq("38;5;111")
MUNI     = _seq("38;5;111")    # azul municipal
TENUE    = _seq("38;5;240")
GRIS     = _seq("38;5;245")
BLANCO   = _seq("38;5;255")

ANCHO_FILETE = 68


def filete(char="═", color=None):
    color = color or ROJO
    return f"{color}{char * ANCHO_FILETE}{RESET}"


def cabecera(texto, subtitulo=""):
    """Cabecera estilo diario: filete doble + título + subtítulo opcional."""
    print()
    print(filete("═"))
    print(f"  {AMARILLO}{BOLD}{texto}{RESET}")
    if subtitulo:
        print(f"  {TENUE}{subtitulo}{RESET}")
    print(filete("═"))
    print()


def fase(texto):
    """Subtítulo de sección."""
    print(f"\n{CYAN}{BOLD}» {texto}{RESET}")


def ok(texto):     return f"{VERDE}{texto}{RESET}"
def err(texto):    return f"{ROJO}{texto}{RESET}"
def tenue(texto):  return f"{TENUE}{texto}{RESET}"
def claro(texto):  return f"{AMARILLO}{BOLD}{texto}{RESET}"
def info(texto):   return f"{CYAN}{texto}{RESET}"
