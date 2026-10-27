# -*- coding: utf-8 -*-
"""
Fabrica de Videos - convierte guiones (carpeta "guiones") en videos listos para YouTube.

Uso:
    python fabrica.py              -> crea todos los videos pendientes
    python fabrica.py --solo 001   -> solo el guion cuyo nombre empieza por 001
    python fabrica.py --rehacer    -> vuelve a crear aunque ya exista
    python fabrica.py --sin-shorts -> solo el video largo
    python fabrica.py --prueba     -> sin internet: voz muda y fondos generados (para probar)
"""
import argparse
import asyncio
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import wave

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
GUIONES = os.path.join(BASE, "guiones")
SALIDA = os.path.join(BASE, "LISTOS PARA PUBLICAR")
CACHE = os.path.join(BASE, "cache")
MIS_IMAGENES = os.path.join(BASE, "mis_imagenes")
FUENTE = os.path.join(BASE, "fuentes", "Poppins-Bold.ttf")
FPS = 30
UA = "FabricaDeVideos/1.0 (canal educativo de historia; uso personal)"


# ----------------------------------------------------------------- utilidades
def cargar_config():
    cfg = {
        "voz": "es-MX-JorgeNeural",
        "velocidad": "-4%",
        "volumen_musica": 0.10,
        "nombre_canal": "Mi canal",
    }
    ruta = os.path.join(BASE, "config.json")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = None


def ff(args, cwd=None):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + args
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError("ffmpeg fallo:\n" + r.stderr[-3000:])


def duracion_wav(ruta):
    with wave.open(ruta, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def fuente(tam):
    return ImageFont.truetype(FUENTE, tam)


def mmss(seg):
    seg = int(seg)
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------------------------------------------------- voz
async def _tts(texto, voz, velocidad, mp3):
    import edge_tts

    try:
        com = edge_tts.Communicate(texto, voz, rate=velocidad, boundary="WordBoundary")
    except TypeError:
        com = edge_tts.Communicate(texto, voz, rate=velocidad)
    marcas = []
    with open(mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("WordBoundary", "SentenceBoundary"):
                ini = ch["offset"] / 1e7
                marcas.append((ini, ini + ch["duration"] / 1e7, ch["text"], ch["type"]))
    return marcas


def repartir(texto, ini, fin):
    """Reparte las palabras de un texto entre ini y fin segun su longitud."""
    pal = texto.split()
    if not pal:
        return []
    total = sum(len(p) + 1 for p in pal)
    t, out = ini, []
    for p in pal:
        d = (fin - ini) * (len(p) + 1) / total
        out.append((t, t + d, p))
        t += d
    return out


def generar_voz(texto, cfg, carpeta, nombre, prueba):
    """Devuelve (wav, duracion, palabras[(ini, fin, palabra)])."""
    wav = os.path.join(carpeta, nombre + ".wav")
    if prueba:
        dur = max(2.0, len(texto.split()) / 2.6)
        ff(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{dur:.2f}", wav])
        return wav, dur, repartir(texto, 0, dur)
    mp3 = os.path.join(carpeta, nombre + ".mp3")
    marcas = None
    for intento in range(3):
        try:
            marcas = asyncio.run(_tts(texto, cfg["voz"], cfg["velocidad"], mp3))
            if os.path.getsize(mp3) > 1000:
                break
        except Exception as e:
            print(f"   (la voz fallo, reintento {intento + 1}/3: {e})")
    else:
        raise RuntimeError("No se pudo generar la voz. Revisa tu conexion a internet.")
    ff(["-i", mp3, "-ar", "48000", "-ac", "2", wav])
    dur = duracion_wav(wav)
    palabras = []
    if marcas and marcas[0][3] == "WordBoundary":
        palabras = [(a, b, t) for a, b, t, _ in marcas]
    elif marcas:
        for i, (a, b, t, _) in enumerate(marcas):
            fin = marcas[i + 1][0] if i + 1 < len(marcas) else dur
            palabras += repartir(t, a, min(fin, b if b > a else fin))
    else:
        palabras = repartir(texto, 0, dur)
    return wav, dur, palabras


def agrupar(palabras, max_pal, inicio_extra):
    grupos, act = [], []
    for i, (a, b, t) in enumerate(palabras):
        act.append((a, b, t))
        sig = palabras[i + 1] if i + 1 < len(palabras) else None
        corte = (
            len(act) >= max_pal
            or t[-1:] in ".,;:?!"
            or sig is None
            or sig[0] - b > 0.35
        )
        if corte:
            grupos.append([act[0][0], act[-1][1], " ".join(x[2] for x in act)])
            act = []
    for i, g in enumerate(grupos):
        if i + 1 < len(grupos) and grupos[i + 1][0] - g[1] < 0.6:
            g[1] = grupos[i + 1][0]
        else:
            g[1] += 0.25
        g[0] += inicio_extra
        g[1] += inicio_extra
    return grupos


# ------------------------------------------------------------------ imagenes
def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _limpiar_html(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


LICENCIAS_OK = ("public domain", "pd", "cc0", "cc by", "cc-by", "no restrictions")


def buscar_commons(consulta, usadas):
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": consulta + " filetype:bitmap", "gsrnamespace": "6", "gsrlimit": "25",
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": "2560",
    }
    datos = _get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params))
    paginas = sorted(datos.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
    for p in paginas:
        info = (p.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        if info.get("width", 0) < 900 or info.get("height", 0) < 500:
            continue
        meta = info.get("extmetadata", {})
        lic = _limpiar_html(meta.get("LicenseShortName", {}).get("value", ""))
        l = lic.lower()
        if re.search(r"(^|[\s-])(nc|nd)([\s-]|$)", l) or not any(k in l for k in LICENCIAS_OK):
            continue
        url = info.get("thumburl") or info.get("url")
        if url in usadas:
            continue
        autor = _limpiar_html(meta.get("Artist", {}).get("value", "")) or "Autor desconocido"
        pagina = info.get("descriptionshorturl") or info.get("descriptionurl", "")
        titulo = os.path.splitext(p.get('title', '').replace('File:', ''))[0][:45]
        return url, f"{titulo} — {autor[:40]} — {lic} — {pagina}"
    return None, None


def fondo_generado(ruta, ancho, alto, semilla):
    rnd = random.Random(semilla)
    paleta = [((10, 14, 30), (40, 60, 90)), ((20, 8, 8), (90, 30, 20)), ((6, 20, 20), (30, 80, 75)),
              ((15, 12, 25), (70, 50, 100))]
    c1, c2 = rnd.choice(paleta)
    peq = Image.new("RGB", (64, 36))
    px = peq.load()
    cx, cy = rnd.uniform(0.2, 0.8), rnd.uniform(0.2, 0.8)
    for y in range(36):
        for x in range(64):
            d = min(1.0, math.hypot(x / 64 - cx, (y / 36 - cy) * 0.6) * 1.6)
            px[x, y] = tuple(int(c2[i] * (1 - d) + c1[i] * d) for i in range(3))
    img = peq.resize((ancho, alto), Image.BICUBIC).filter(ImageFilter.GaussianBlur(30))
    ruido = Image.effect_noise((ancho // 2, alto // 2), 18).resize((ancho, alto)).convert("RGB")
    img = Image.blend(img, ruido, 0.06)
    img.save(ruta, quality=92)


def obtener_imagen(escena, usadas, creditos, prueba, semilla):
    """Devuelve la ruta de una imagen original para la escena."""
    os.makedirs(os.path.join(CACHE, "imagenes"), exist_ok=True)
    local = escena.get("imagen_local")
    if local and os.path.exists(os.path.join(MIS_IMAGENES, local)):
        return os.path.join(MIS_IMAGENES, local)
    consulta = escena.get("imagen", "")
    if consulta and not prueba:
        for c in [consulta] + escena.get("imagen_alternativas", []):
            try:
                url, credito = buscar_commons(c, usadas)
            except Exception as e:
                print(f"   (no se pudo buscar imagen '{c}': {e})")
                url = None
            if url:
                ruta = os.path.join(CACHE, "imagenes", hashlib.md5(url.encode()).hexdigest() + ".jpg")
                try:
                    if not os.path.exists(ruta):
                        req = urllib.request.Request(url, headers={"User-Agent": UA})
                        with urllib.request.urlopen(req, timeout=60) as r, open(ruta, "wb") as f:
                            f.write(r.read())
                    Image.open(ruta).convert("RGB")
                    usadas.add(url)
                    if credito not in creditos:
                        creditos.append(credito)
                    return ruta
                except Exception as e:
                    print(f"   (descarga fallida: {e})")
    ruta = os.path.join(CACHE, "imagenes", f"fondo_{semilla}.jpg")
    if not os.path.exists(ruta):
        fondo_generado(ruta, 1920, 1080, semilla)
    return ruta


def viñeta(img, fuerza=0.55):
    w, h = img.size
    m = Image.new("L", (64, 64))
    px = m.load()
    for y in range(64):
        for x in range(64):
            d = math.hypot((x - 31.5) / 32, (y - 31.5) / 32) / 1.414
            px[x, y] = int(255 * min(1, max(0, (d - 0.35) / 0.65)) * fuerza)
    m = m.resize((w, h), Image.BICUBIC)
    return Image.composite(Image.new("RGB", (w, h)), img, m)


def gradar(img):
    img = ImageEnhance.Color(img).enhance(0.72)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Brightness(img).enhance(0.88)
    tinte = Image.new("RGB", img.size, (20, 35, 55))
    return Image.blend(img, tinte, 0.10)


def preparar_imagen(origen, destino, vertical):
    img = ImageOps.exif_transpose(Image.open(origen)).convert("RGB")
    if not vertical:
        w, h = 2880, 1620
        lienzo = ImageOps.fit(img, (w, h), Image.LANCZOS)
    else:
        w, h = 1620, 2880
        fondo = ImageOps.fit(img, (w // 4, h // 4), Image.LANCZOS).filter(ImageFilter.GaussianBlur(12))
        lienzo = ImageEnhance.Brightness(fondo.resize((w, h), Image.BICUBIC)).enhance(0.45)
        frente = ImageOps.contain(img, (w, int(h * 0.62)), Image.LANCZOS)
        if frente.width < w:
            frente = ImageOps.fit(img, (w, min(int(h * 0.62), int(w * img.height / img.width))), Image.LANCZOS)
        lienzo.paste(frente, ((w - frente.width) // 2, int(h * 0.40 - frente.height / 2)))
    lienzo = viñeta(gradar(lienzo))
    lienzo.save(destino, quality=92)
    return destino


# ------------------------------------------------------------------- textos
def imagen_subtitulo(texto, ruta, vertical):
    tam = 92 if vertical else 62
    ancho_max = 960 if vertical else 1650
    texto = texto.upper() if vertical else texto
    f = fuente(tam)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lineas, act = [], ""
    for p in texto.split():
        prueba = (act + " " + p).strip()
        if tmp.textlength(prueba, font=f) <= ancho_max or not act:
            act = prueba
        else:
            lineas.append(act)
            act = p
    lineas.append(act)
    borde = 9 if vertical else 6
    alto_l = int(tam * 1.18)
    w = int(max(tmp.textlength(l, font=f) for l in lineas)) + borde * 4
    h = alto_l * len(lineas) + borde * 4
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (255, 214, 0) if vertical else (255, 255, 255)
    for i, l in enumerate(lineas):
        x = (w - d.textlength(l, font=f)) / 2
        d.text((x, borde * 2 + i * alto_l), l, font=f, fill=color, stroke_width=borde, stroke_fill=(0, 0, 0))
    img.save(ruta)


def miniatura(origen, texto, ruta):
    img = ImageOps.fit(Image.open(origen).convert("RGB"), (1280, 720), Image.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    sombra = Image.new("L", (1280, 720))
    sp = sombra.load()
    for x in range(1280):
        v = int(max(0, 1 - x / 900) * 200)
        for y in range(720):
            sp[x, y] = v
    img = Image.composite(Image.new("RGB", img.size), img, sombra)
    d = ImageDraw.Draw(img)
    palabras = texto.upper().split()
    lineas, act = [], []
    for p in palabras:
        if len(" ".join(act + [p])) > 12 and act:
            lineas.append(" ".join(act))
            act = [p]
        else:
            act.append(p)
    lineas.append(" ".join(act))
    tam = 150
    while tam > 50:
        f = fuente(tam)
        if max(d.textlength(l, font=f) for l in lineas) <= 760 and tam * 1.05 * len(lineas) <= 600:
            break
        tam -= 6
    f = fuente(tam)
    y = (720 - tam * 1.05 * len(lineas)) / 2
    for i, l in enumerate(lineas):
        color = (255, 214, 0) if i == 0 else (255, 255, 255)
        d.text((60, y), l, font=f, fill=color, stroke_width=10, stroke_fill=(0, 0, 0))
        y += tam * 1.05
    d.rectangle([0, 700, 1280, 720], fill=(200, 20, 20))
    img.save(ruta, quality=92)


# ------------------------------------------------------------------- render
MOVIMIENTOS = ["acercar", "alejar", "derecha", "izquierda"]


def zoom_expr(mov, n):
    c = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if mov == "acercar":
        return f"1+0.16*on/{n}", c[0], c[1]
    if mov == "alejar":
        return f"1.16-0.16*on/{n}", c[0], c[1]
    if mov == "derecha":
        return "1.14", f"(iw-iw/zoom)*on/{n}", c[1]
    return "1.14", f"(iw-iw/zoom)*(1-on/{n})", c[1]


def render_escena(img, wav, dur_voz, subtitulos, salida, vertical, mov, tmpdir, idx):
    lead, cola = 0.25, 0.45
    dur = dur_voz + lead + cola
    n = int(round(dur * FPS))
    W, H = (1080, 1920) if vertical else (1920, 1080)
    z, x, y = zoom_expr(mov, n)
    entradas = ["-i", img, "-i", wav]
    cadena = [f"[0:v]zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={W}x{H}:fps={FPS},"
              f"fade=t=in:st=0:d=0.3,format=rgba[v0]"]
    ult = "v0"
    pos_y = int(H * 0.72) if vertical else int(H * 0.80)
    for k, (a, b, t) in enumerate(subtitulos):
        png = os.path.join(tmpdir, f"sub_{idx}_{k}.png")
        imagen_subtitulo(t, png, vertical)
        entradas += ["-i", png]
        cadena.append(f"[{ult}][{k + 2}:v]overlay=x=(W-w)/2:y={pos_y}-h/2:"
                      f"enable='between(t,{a:.3f},{b:.3f})'[o{k}]")
        ult = f"o{k}"
    cadena.append(f"[{ult}]format=yuv420p[vout]")
    ms = int(lead * 1000)
    cadena.append(f"[1:a]adelay={ms}|{ms},apad[aout]")
    script = os.path.join(tmpdir, f"filtro_{idx}.txt")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(";\n".join(cadena))
    ff(entradas + ["-filter_complex_script", script, "-map", "[vout]", "-map", "[aout]",
                   "-t", f"{dur:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", salida])
    return dur


def musica(ruta, dur, semilla):
    rnd = random.Random(semilla)
    raiz = rnd.choice([55.0, 49.0, 41.2, 61.74])
    q = [raiz, raiz * 1.5, raiz * 2, raiz * 2.4]
    l = "+".join(f"0.18*sin(2*PI*{f:.2f}*t)*(0.55+0.45*sin(2*PI*{rnd.uniform(0.02, 0.08):.3f}*t+{i}))"
                 for i, f in enumerate(q))
    r = "+".join(f"0.18*sin(2*PI*{f * 1.003:.2f}*t)*(0.55+0.45*sin(2*PI*{rnd.uniform(0.02, 0.08):.3f}*t+{i + 1}))"
                 for i, f in enumerate(q))
    ff(["-f", "lavfi", "-i", f"aevalsrc='{l}|{r}':s=48000:d={dur:.2f}",
        "-f", "lavfi", "-i", f"anoisesrc=color=brown:amplitude=0.12:d={dur:.2f}:r=48000",
        "-filter_complex",
        "[1:a]lowpass=f=300,pan=stereo|c0=c0|c1=c0[n];[0:a][n]amix=inputs=2:normalize=0,"
        f"lowpass=f=1400,afade=t=in:d=3,afade=t=out:st={max(0, dur - 4):.2f}:d=4,volume=0.6[m]",
        "-map", "[m]", ruta])


def construir(escenas, cfg, tmpdir, vertical, prueba, creditos, usadas, semilla_base, prefijo):
    clips, capitulos, t = [], [], 0.0
    rnd = random.Random(semilla_base)
    previo = None
    for i, esc in enumerate(escenas):
        print(f"   escena {i + 1}/{len(escenas)}: {esc['texto'][:55]}...")
        if esc.get("capitulo"):
            capitulos.append((t, esc["capitulo"]))
        wav, dur_voz, palabras = generar_voz(esc["texto"], cfg, tmpdir, f"{prefijo}_voz_{i}", prueba)
        subs = agrupar(palabras, 2 if vertical else 5, 0.25)
        orig = obtener_imagen(esc, usadas, creditos, prueba, semilla_base * 100 + i)
        img = preparar_imagen(orig, os.path.join(tmpdir, f"{prefijo}_img_{i}.jpg"), vertical)
        mov = rnd.choice([m for m in MOVIMIENTOS if m != previo])
        previo = mov
        clip = os.path.join(tmpdir, f"{prefijo}_clip_{i:03d}.mp4")
        t += render_escena(img, wav, dur_voz, subs, clip, vertical, mov, tmpdir, f"{prefijo}{i}")
        clips.append(os.path.basename(clip))
    lista = os.path.join(tmpdir, f"{prefijo}_lista.txt")
    with open(lista, "w", encoding="utf-8") as fh:
        fh.write("".join(f"file '{c}'\n" for c in clips))
    unido = f"{prefijo}_unido.mp4"
    ff(["-f", "concat", "-safe", "0", "-i", os.path.basename(lista), "-c", "copy", unido], cwd=tmpdir)
    return os.path.join(tmpdir, unido), t, capitulos


def mezclar_final(unido, dur, cfg, tmpdir, salida, semilla):
    m = os.path.join(tmpdir, f"musica_{semilla}.wav")
    musica(m, dur, semilla)
    vol = float(cfg.get("volumen_musica", 0.10))
    ff(["-i", unido, "-i", m, "-filter_complex",
        f"[1:a]volume={vol}[mu];[0:a][mu]amix=inputs=2:duration=first:normalize=0,"
        "loudnorm=I=-14:TP=-1.5:LRA=11[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-ar", "48000", "-movflags", "+faststart", salida])


# --------------------------------------------------------------------- main
def procesar(ruta_guion, cfg, args):
    with open(ruta_guion, encoding="utf-8") as f:
        g = json.load(f)
    gid = g.get("id") or os.path.splitext(os.path.basename(ruta_guion))[0]
    destino = os.path.join(SALIDA, gid)
    final = os.path.join(destino, "video.mp4")
    if os.path.exists(final) and not args.rehacer:
        print(f"[saltado] {gid} ya esta listo (usa --rehacer para repetirlo)")
        return
    print(f"\n=== {g['titulo']} ===")
    os.makedirs(destino, exist_ok=True)
    tmpdir = os.path.join(CACHE, "trabajo_" + gid)
    if os.path.exists(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    semilla = int(hashlib.md5(gid.encode()).hexdigest()[:6], 16)
    creditos, usadas = [], set()

    print(" > Video largo")
    unido, dur, capitulos = construir(g["escenas"], cfg, tmpdir, False, args.prueba,
                                      creditos, usadas, semilla, "largo")
    print(" > Mezclando voz y musica")
    mezclar_final(unido, dur, cfg, tmpdir, final, semilla)

    idx_m = int(g.get("miniatura_escena", 0))
    miniatura(os.path.join(tmpdir, f"largo_img_{idx_m}.jpg"), g.get("texto_miniatura", g["titulo"]),
              os.path.join(destino, "miniatura.jpg"))

    desc = g.get("descripcion", "").strip()
    if len(capitulos) >= 3:
        desc += "\n\n⏱ Capítulos\n" + "\n".join(
            f"{mmss(0 if k == 0 else c)} {n}" for k, (c, n) in enumerate(capitulos))
    if g.get("fuentes"):
        desc += "\n\n📚 Fuentes\n" + "\n".join("• " + s for s in g["fuentes"])
    texto_creditos = ""
    if creditos:
        texto_creditos = "\n\n🖼 Imágenes (Wikimedia Commons)\n" + "\n".join("• " + c for c in creditos)
    if len(desc) + len(texto_creditos) > 4800:
        texto_creditos = re.sub(r" — https://\S+", "", texto_creditos)
    if len(desc) + len(texto_creditos) > 4800:
        texto_creditos = "\n\n🖼 Imágenes: Wikimedia Commons (dominio público y licencias Creative Commons)."
    desc += texto_creditos
    escribir_ficha(os.path.join(destino, "PUBLICAR.txt"), g["titulo"], desc, g.get("etiquetas", []), dur, False)
    meta = {"id": gid, "titulo": g["titulo"], "descripcion": desc, "etiquetas": g.get("etiquetas", []),
            "duracion": round(dur), "shorts": []}

    if not args.sin_shorts:
        for k, s in enumerate(g.get("shorts", []), 1):
            print(f" > Short {k}")
            cs = []
            unido_s, dur_s, _ = construir(s["escenas"], cfg, tmpdir, True, args.prueba,
                                          cs, usadas, semilla + k, f"short{k}")
            mezclar_final(unido_s, dur_s, cfg, tmpdir, os.path.join(destino, f"short_{k}.mp4"), semilla + k)
            ds = s.get("descripcion", "").strip()
            if cs:
                ds += "\n\n🖼 Imágenes: Wikimedia Commons\n" + "\n".join("• " + c for c in cs)
            escribir_ficha(os.path.join(destino, f"short_{k}_PUBLICAR.txt"), s["titulo"], ds,
                           s.get("etiquetas", g.get("etiquetas", [])[:8]), dur_s, True)
            meta["shorts"].append({"archivo": f"short_{k}.mp4", "titulo": s["titulo"], "descripcion": ds,
                                   "etiquetas": s.get("etiquetas", g.get("etiquetas", [])[:8]),
                                   "duracion": round(dur_s)})
            if dur_s > 179:
                print(f"   AVISO: el short {k} dura {dur_s:.0f}s (YouTube Shorts admite hasta 3 min)")
    with open(os.path.join(destino, "datos.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"[listo] {destino}")


def escribir_ficha(ruta, titulo, desc, etiquetas, dur, es_short):
    with open(ruta, "w", encoding="utf-8") as f:
        f.write("TÍTULO (cópialo tal cual)\n" + "-" * 40 + "\n" + titulo + "\n\n")
        f.write("DESCRIPCIÓN\n" + "-" * 40 + "\n" + desc + "\n\n")
        f.write("ETIQUETAS (pegar en 'Etiquetas')\n" + "-" * 40 + "\n" + ", ".join(etiquetas) + "\n\n")
        f.write(f"Duración: {mmss(dur)}\n\n")
        f.write("AL SUBIR:\n• Audiencia: 'No, no es contenido creado para niños'\n")
        if not es_short:
            f.write("• Sube también 'miniatura.jpg' como miniatura personalizada\n")
        f.write("• Categoría: Educación\n• Publica a la hora indicada en el calendario\n")


def main():
    global FFMPEG
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo")
    ap.add_argument("--rehacer", action="store_true")
    ap.add_argument("--sin-shorts", action="store_true")
    ap.add_argument("--prueba", action="store_true")
    args = ap.parse_args()
    FFMPEG = ffmpeg_exe()
    cfg = cargar_config()
    os.makedirs(SALIDA, exist_ok=True)
    os.makedirs(MIS_IMAGENES, exist_ok=True)
    guiones = sorted(f for f in os.listdir(GUIONES) if f.endswith(".json"))
    if args.solo:
        guiones = [f for f in guiones if f.startswith(args.solo)]
    if not guiones:
        print("No hay guiones en la carpeta 'guiones'.")
        return
    errores = 0
    for gname in guiones:
        try:
            procesar(os.path.join(GUIONES, gname), cfg, args)
        except Exception as e:
            errores += 1
            print(f"\n[ERROR] {gname}: {e}")
    print("\nTerminado." + (f" {errores} guion(es) con error." if errores else " Todo salió bien."))
    if errores:
        sys.exit(1)


if __name__ == "__main__":
    main()
