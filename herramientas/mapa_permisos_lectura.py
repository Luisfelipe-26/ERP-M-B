"""Genera permisos_lectura.py: qué módulos pueden leer cada ruta GET del API.

Uso:  python herramientas/mapa_permisos_lectura.py ../ERP-M-F

Una ruta la pueden leer los módulos de las pantallas que la usan. Se toma toda ruta del API
que aparezca escrita en el archivo de la pantalla o en los componentes que importa, aunque
se arme en una variable (`url = \\`/contabilidad/...\\``): si sobra, solo amplía quién lee;
nunca bloquea una pantalla. Lo que se usa desde el menú o la sesión queda abierto ("*").
Volver a generarlo al agregar pantallas o rutas que se leen desde otro módulo.
"""
import ast
import pathlib
import re
import sys

B = pathlib.Path(__file__).resolve().parent.parent
F = pathlib.Path(sys.argv[1]).resolve() / "src"
GLOBALES = {"components/Layout.tsx", "contexts/AuthContext.tsx", "App.tsx", "main.tsx", "hooks/useApi.ts"}


def nombre(n):
    if isinstance(n, ast.Attribute):
        return f"{nombre(n.value)}.{n.attr}"
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Call):
        return nombre(n.func)
    return ""


# ── Rutas GET del backend ──
rutas = []
for f in sorted((B / "routers").glob("*.py")):
    if f.stem in ("__init__", "auth"):
        continue
    arbol = ast.parse(f.read_text(encoding="utf-8"))
    pref = ""
    for n in ast.walk(arbol):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and nombre(n.value.func) == "APIRouter":
            pref = next((kw.value.value for kw in n.value.keywords if kw.arg == "prefix"), "")
    for n in ast.walk(arbol):
        if isinstance(n, ast.FunctionDef):
            for dec in n.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) \
                        and nombre(dec.func.value) == "router" and dec.func.attr == "get":
                    rutas.append(pref + (dec.args[0].value if dec.args else ""))
primeros = {r.removeprefix("/api/").split("/")[0] for r in rutas}

# ── Pantalla → módulo (App.tsx) ──
app = (F / "App.tsx").read_text(encoding="utf-8")
comp_archivo = dict(re.findall(r"const (\w+) = lazy\(\(\) => import\('\./pages/([^']+)'\)\)", app))
modulo_de = {}
for mod, comp in re.findall(r'<PermissionRoute modulo="(\w+)"><(\w+) />', app):
    modulo_de.setdefault(f"pages/{comp_archivo[comp]}.tsx", set()).add(mod)
modulo_de["pages/Dashboard.tsx"] = {"dashboard"}     # inicio: lo ve todo usuario, sus datos son del módulo Dashboard
for perms, comp in re.findall(r"<PermissionRoute permisos=\{\[([^\]]+)\]\}><(\w+) />", app):
    modulo_de.setdefault(f"pages/{comp_archivo[comp]}.tsx", set()).update(
        p.strip(" '").split(".")[0] for p in perms.split(","))

# ── Componentes: heredan los módulos de quien los importa ──
archivos = {p.relative_to(F).as_posix(): p.read_text(encoding="utf-8") for p in F.rglob("*.ts*")}
importa = {}
for rel, txt in archivos.items():
    base = pathlib.PurePosixPath(rel).parent
    for imp in re.findall(r"from\s+'(\.[^']+)'", txt):
        destino = (base / imp).as_posix()
        partes = []
        for p in destino.split("/"):
            if p == "..":
                partes.pop()
            elif p != ".":
                partes.append(p)
        destino = "/".join(partes)
        for ext in (".tsx", ".ts", "/index.tsx", "/index.ts"):
            if destino + ext in archivos:
                importa.setdefault(destino + ext, set()).add(rel)
modulos = {k: set(v) for k, v in modulo_de.items()}
for _ in range(10):                                  # propaga por la cadena de imports
    for comp, quienes in importa.items():
        if comp in GLOBALES or comp == "api.ts":
            continue
        for q in quienes:
            modulos.setdefault(comp, set()).update(modulos.get(q, set()))
modulos["api.ts"] = {"ordenes"}                      # planificacionesApi: pantalla /planificacion

# ── Rutas del API escritas en cada archivo ──
lit = re.compile(r"[`'\"](/(?:%s)(?:/[^`'\"\s]*)?)[`'\"]" % "|".join(sorted(map(re.escape, primeros), key=len, reverse=True)))
# Los enlaces a pantallas (`to="/campos"`, `navigate('/ventas')`) se llaman igual que rutas
# del API, pero no leen nada. En el menú y la sesión solo cuentan las llamadas explícitas.
ENLACE = re.compile(r"(\bto\s*[=:]\s*\{?|navigate\(\s*|href\s*=\s*\{?|path\s*[=:]\s*)$")
LLAMADA = re.compile(r"\bapi\.(get|post|put|patch|delete)(<[^>]*>)?\(\s*$")
usos = []                                            # (segmentos, archivo)
for rel, txt in archivos.items():
    for m in lit.finditer(txt):
        antes = txt[max(0, m.start() - 40):m.start()]
        if ENLACE.search(antes) or (rel in GLOBALES and not LLAMADA.search(antes)):
            continue
        url = re.sub(r"\$\{[^}]*\}", "{x}", m.group(1)).split("?")[0].rstrip("/")
        usos.append((url.strip("/").split("/"), rel))


def coincide(back, front):
    if len(back) != len(front):
        return False
    return all(b.startswith("{") or f.startswith("{") or b == f for b, f in zip(back, front))


salida = {}
for r in sorted(set(rutas)):
    seg = r.removeprefix("/api/").strip("/").split("/")
    quienes = {a for s, a in usos if coincide(seg, s)}
    if not quienes:
        continue                                     # nadie la lee desde una pantalla: su router decide
    if quienes & GLOBALES:
        salida[r] = "*"
        continue
    mods = set().union(*(modulos.get(a, set()) for a in quienes))
    if mods:
        salida[r] = tuple(sorted(mods))

lineas = ['"""Generado por herramientas/mapa_permisos_lectura.py a partir del frontend (ERP-M-F).',
          "",
          "Ruta GET → módulos cuyas pantallas la leen; \"*\" = cualquier usuario con sesión. Se suman",
          "a los módulos de su router (main.PERMISOS_MODULO), que siempre pueden leer sus rutas.",
          '"""', "", "LECTURAS = {"]
for r, m in salida.items():
    lineas.append(f'    "{r}": {m!r},')
lineas.append("}")
(B / "permisos_lectura.py").write_text("\n".join(lineas) + "\n", encoding="utf-8")
print(f"rutas GET: {len(set(rutas))}  en el mapa: {len(salida)}  abiertas: {sum(1 for v in salida.values() if v == '*')}")
