import argparse
import base64
import copy
import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from lxml import etree
from PIL import Image, features

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "tmp"
CHECK_DIR = TMP / "bake-check"
NS = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
RENDER_SCALE = 2
DEFAULT_SCALE = 1.0
DEFAULT_MASK_SCALE = 0.5
DEFAULT_QUALITY = 80
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
NON_RENDERING = {"defs", "clipPath", "mask", "linearGradient", "radialGradient", "pattern", "filter", "symbol",
                 "marker", "title", "desc", "metadata", "style"}
PAINTING = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "image", "use"}
INHERITED = {"fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap",
             "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "color", "font-family",
             "font-size", "font-weight", "font-style", "letter-spacing", "word-spacing", "text-anchor", "visibility",
             "clip-rule", "color-interpolation", "color-interpolation-filters", "paint-order"}
BAKED_EFFECTS = ("filter", "opacity")
LIVE_EFFECTS = ("mask", "clip-path")
URL_REF = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)")
CHECK_TILE = 40
CHECK_MIN_TILE_PSNR = 20.0
CHECK_BACKGROUNDS = ("#0d1117", "#ffffff")
CHROMIUM_TIMEOUT = 120


class BakeError(Exception):
    pass


def q(tag):
    return "{%s}%s" % (NS, tag)


def local(el):
    return etree.QName(el).localname


def elements(el):
    return [c for c in el if isinstance(c.tag, str)]


def where(el):
    return f"line {el.sourceline}" if el.sourceline else f"<{local(el)}>"


def find_chromium():
    explicit = os.environ.get("CHROMIUM")
    if explicit:
        path = shutil.which(explicit)
        if not path:
            raise BakeError(f"CHROMIUM={explicit} is not an executable")
        return path
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    raise BakeError("Chromium not found: install it or set CHROMIUM=/path/to/chrome")


def screenshot(svg_bytes, size, work, name, scale, background="transparent"):
    w, h = size
    svg = work / f"{name}.svg"
    svg.write_bytes(svg_bytes)
    html = work / f"{name}.html"
    html.write_text(f'<html><body style="margin:0;background:{background}"><img src="{svg.name}" width="{w}" height="{h}" style="display:block"></body></html>')
    png = work / f"{name}.png"
    profile = work / f"profile-{name}"
    cmd = [find_chromium(), "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
           f"--force-device-scale-factor={scale}", "--default-background-color=00000000",
           f"--window-size={w},{h}", f"--screenshot={png}", html.as_uri()]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CHROMIUM_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BakeError(f"Chromium took more than {CHROMIUM_TIMEOUT}s rendering {name}")
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if result.returncode != 0 or not png.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise BakeError(f"Chromium failed rendering {name} ({cmd[0]}):\n{tail}")
    return Image.open(png).convert("RGBA")


def parse_transform(text):
    m = np.identity(3)
    for fn, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        a = [float(v) for v in re.split(r"[\s,]+", args.strip()) if v]
        if fn == "matrix":
            t = np.array([[a[0], a[2], a[4]], [a[1], a[3], a[5]], [0, 0, 1]])
        elif fn == "translate":
            t = np.array([[1, 0, a[0]], [0, 1, a[1] if len(a) > 1 else 0], [0, 0, 1]])
        elif fn == "scale":
            t = np.diag([a[0], a[1] if len(a) > 1 else a[0], 1])
        elif fn == "rotate":
            r = math.radians(a[0])
            c, s = math.cos(r), math.sin(r)
            t = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
            if len(a) == 3:
                t = parse_transform(f"translate({a[1]} {a[2]})") @ t @ parse_transform(f"translate({-a[1]} {-a[2]})")
        elif fn == "skewX":
            t = np.array([[1, math.tan(math.radians(a[0])), 0], [0, 1, 0], [0, 0, 1]])
        elif fn == "skewY":
            t = np.array([[1, 0, 0], [math.tan(math.radians(a[0])), 1, 0], [0, 0, 1]])
        else:
            raise BakeError(f"unsupported transform {fn}()")
        m = m @ t
    return m


def ctm(el):
    m = np.identity(3)
    for anc in reversed(list(el.iterancestors())):
        m = m @ parse_transform(anc.get("transform"))
    return m


def canvas_size(root):
    try:
        w, h = int(root.get("width")), int(root.get("height"))
        vb = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox").strip())]
    except (TypeError, ValueError, AttributeError):
        vb, w, h = None, 0, 0
    if w <= 0 or h <= 0 or vb != [0, 0, w, h]:
        raise BakeError('the root <svg> needs integer width and height and viewBox="0 0 width height"')
    return w, h


KEYFRAMES = r"@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*[^{}]*)\}"


def css_text(root):
    return "\n".join(st.text or "" for st in root.iter(q("style")))


def split_css(css):
    keyframes = dict(re.findall(KEYFRAMES, css))
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(KEYFRAMES, "", css))
    return keyframes, rules


def css_list(value):
    return [v.strip() for v in re.split(r",(?![^()]*\))", value) if v.strip()]


def seconds(token):
    found = re.fullmatch(r"(\d*\.?\d+)(ms|s)", token)
    return float(found.group(1)) / (1000 if found.group(2) == "ms" else 1) if found else None


def animations(css):
    keyframes, rules = split_css(css)
    found = []
    for selector, body in rules:
        decls = dict((k.strip(), v.strip()) for k, v in re.findall(r"([\w-]+)\s*:([^;]*)", body))
        items = []
        if "animation" in decls:
            for item in css_list(decls["animation"]):
                tokens = re.findall(r"[\w.-]+\([^)]*\)|[^\s]+", item)
                times = [seconds(t) for t in tokens if seconds(t) is not None]
                timing = next((t for t in tokens if t.startswith(("steps(", "cubic-bezier(")) or t in (
                    "ease", "linear", "ease-in", "ease-out", "ease-in-out", "step-start", "step-end")), "ease")
                items.append((next((t for t in tokens if t in keyframes), None), times[0] if times else None, timing))
        if "animation-name" in decls:
            names = css_list(decls["animation-name"])
            durations = [seconds(d) for d in css_list(decls.get("animation-duration", "0s"))]
            timings = css_list(decls.get("animation-timing-function", "ease"))
            items = [(n, durations[i % len(durations)], timings[i % len(timings)]) for i, n in enumerate(names)]
        for name, duration, timing in items:
            if name not in keyframes or not duration:
                raise BakeError(f'cannot read the animation in the CSS rule "{selector.strip()}": '
                                "give it a @keyframes name and a duration")
            found.append({"selector": selector, "name": name, "duration": duration,
                          "stepped": timing.startswith("steps(") or timing in ("step-start", "step-end"),
                          "moves": "transform" in keyframes[name]})
    return found, keyframes


def styled_selectors(css):
    _, rules = split_css(css)
    classes = {c for selector, _ in rules for c in re.findall(r"\.([\w-]+)", selector)}
    ids = {i for selector, _ in rules for i in re.findall(r"#([\w-]+)", selector)}
    anims, _ = animations(css)
    moving = {s for a in anims if a["moves"] for s in re.findall(r"[.#][\w-]+", a["selector"])}
    return classes, ids, moving


def check_times(css):
    anims, keyframes = animations(css)
    times = set()
    for a in anims:
        stops = {0.0, 100.0}
        for selector in re.findall(r"([^{}]+)\{", keyframes[a["name"]]):
            for part in selector.split(","):
                part = part.strip()
                stops.add(0.0 if part == "from" else 100.0 if part == "to" else float(part.rstrip("%")))
        stops = sorted(stops)
        times.update(round(a["duration"] * (x + y) / 200, 3) for x, y in zip(stops, stops[1:]))
    return sorted(times)


def references(el):
    found = set()
    for key, value in el.attrib.items():
        if key in ("href", XLINK_HREF) and value.startswith("#"):
            found.add(value[1:])
        found.update(URL_REF.findall(value))
    if el.tag == q("style"):
        found.update(URL_REF.findall(el.text or ""))
    return found


def reachable_ids(root, starts):
    by_id = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    seen, queue = set(), [d for s in starts for d in s.iter() if isinstance(d.tag, str)]
    while queue:
        for ident in references(queue.pop()):
            if ident not in seen and ident in by_id:
                seen.add(ident)
                queue.extend(d for d in by_id[ident].iter() if isinstance(d.tag, str))
    return seen


def read_settings(els, label, default_scale):
    result = []
    for key, default, cast, valid, rule in (
            ("data-bake-scale", default_scale, float, lambda v: 0 < v <= 1, "a number in (0, 1]"),
            ("data-bake-quality", DEFAULT_QUALITY, int, lambda v: 0 <= v <= 100, "an integer from 0 to 100")):
        values = {el.get(key) for el in els if el.get(key) is not None}
        if len(values) > 1:
            raise BakeError(f"{label}: {key} differs between its elements: {sorted(values)}")
        if not values:
            result.append(default)
            continue
        raw = values.pop()
        try:
            value = cast(raw)
        except ValueError:
            value = None
        if value is None or not valid(value):
            raise BakeError(f'{label}: {key}="{raw}" must be {rule}')
        result.append(value)
    return result


class Layer:
    def __init__(self, name):
        self.name = name
        self.elements = []
        self.children = []
        self.parent = None
        self.scale = DEFAULT_SCALE
        self.quality = DEFAULT_QUALITY

    @property
    def first(self):
        return self.elements[0]

    @property
    def top(self):
        return self.parent or self


def collect_layers(root):
    layers = {}
    for el in root.iter():
        if not isinstance(el.tag, str) or el.get("data-bake") is None:
            continue
        name = el.get("data-bake")
        if not NAME_PATTERN.fullmatch(name):
            raise BakeError(f'{where(el)}: data-bake="{name}" must use only letters, digits, "-" and "_"')
        for anc in el.iterancestors():
            if local(anc) in NON_RENDERING:
                raise BakeError(f'{where(el)}: layer "{name}" is inside <{local(anc)}>: mark the element where it is drawn')
            if local(anc) == "svg" and anc is not root:
                raise BakeError(f'{where(el)}: layer "{name}" is inside a nested <svg>, which is not supported')
        layers.setdefault(name, Layer(name)).elements.append(el)

    for layer in layers.values():
        layer.scale, layer.quality = read_settings(layer.elements, f'layer "{layer.name}"', DEFAULT_SCALE)
        outer = set()
        for el in layer.elements:
            nearest = next((a for a in el.iterancestors() if a.get("data-bake") is not None), None)
            if nearest is not None and nearest.get("data-bake") == layer.name:
                raise BakeError(f'{where(el)}: layer "{layer.name}" is nested inside itself')
            outer.add(None if nearest is None else nearest.get("data-bake"))
        if len(outer) > 1:
            levels = ", ".join(sorted(n or "top level" for n in outer))
            raise BakeError(f'layer "{layer.name}" has elements at different nesting levels ({levels}): '
                            "all must be top level or inside the same layer")
        parent = outer.pop()
        if parent:
            layer.parent = layers[parent]
            layer.parent.children.append(layer)

    for layer in layers.values():
        if layer.parent and layer.parent.parent:
            raise BakeError(f'layer "{layer.name}" is nested two levels deep (in "{layer.parent.name}", '
                            f'in "{layer.parent.parent.name}"): only one level of nesting is supported')
        if layer.parent is None:
            parent = layer.first.getparent()
            if any(el.getparent() is not parent for el in layer.elements):
                raise BakeError(f'elements of top-level layer "{layer.name}" must be siblings')
            siblings = elements(parent)
            span = siblings[siblings.index(layer.first):siblings.index(layer.elements[-1]) + 1]
            stray = [s for s in span if s not in layer.elements and local(s) not in NON_RENDERING]
            if stray:
                raise BakeError(f'{where(stray[0])}: <{local(stray[0])}> sits between elements of layer "{layer.name}": '
                                "same-name elements must be consecutive")
        ids = [el.get("id") for el in layer.elements if el.get("id")]
        if len(ids) > 1:
            raise BakeError(f'layer "{layer.name}" has more than one id: {ids}')
        if len({el.get("class") or "" for el in layer.elements}) > 1:
            raise BakeError(f'elements of layer "{layer.name}" have different classes')
    return layers


def live_ancestors(layer):
    return [a for el in layer.top.elements for a in el.iterancestors()]


def check_rules(root, layers, masks):
    css = css_text(root)
    timings = [a["selector"].strip() for a in animations(css)[0] if not a["stepped"]]
    timings += [v.strip() for v in re.findall(r"animation-timing-function\s*:([^;}]*)", css)
                if not all(t.startswith("steps(") or t in ("step-start", "step-end") for t in css_list(v))]
    if timings:
        raise BakeError(f'continuous animation timing in "{timings[0]}" redraws the whole image on every frame: use steps(n)')
    for el in root.iter(q("script"), q("foreignObject")):
        raise BakeError(f"{where(el)}: <{local(el)}> does not work in a README image")

    classes, ids, moving = styled_selectors(css)
    scopes = [(f'layer "{layer.name}"', el) for layer in layers.values() for el in layer.elements]
    scopes += [(f"mask #{ident}", mask) for ident, (mask, _, _) in masks.items()]
    for label, el in scopes:
        stack = elements(el) + ([el] if local(el) == "mask" else [])
        while stack:
            d = stack.pop()
            if d.get("data-bake") is not None:
                continue
            hit = {"." + c for c in (d.get("class") or "").split() if c in classes}
            if d.get("id") in ids:
                hit.add("#" + d.get("id"))
            if hit:
                raise BakeError(f'{where(d)}: {" ".join(sorted(hit))} is styled by CSS but sits inside {label}, '
                                "where it would be frozen: give it its own data-bake, move it out, or drop it")
            stack.extend(elements(d))

    for layer in layers.values():
        for el in layer.elements:
            selectors = {"." + c for c in (el.get("class") or "").split()} | ({"#" + el.get("id")} if el.get("id") else set())
            if el.get("transform") and selectors & moving:
                raise BakeError(f'{where(el)}: layer "{layer.name}" has a transform attribute and a CSS animation that '
                                "moves it; after baking they would add up: put data-bake on a child group instead")
            if layer.parent is not None:
                continue
            live_seen = False
            for anc in el.iterancestors():
                check_ancestor(anc, live_seen)
                live_seen = live_seen or any(anc.get(k) for k in LIVE_EFFECTS)


def check_ancestor(anc, live_seen):
    if re.search(r"(^|;)\s*(opacity|filter|mask|clip-path|transform)\s*:", anc.get("style") or ""):
        raise BakeError(f"{where(anc)}: move opacity/filter/mask/clip-path/transform from style= to attributes "
                        "on elements above baked layers")
    if anc.get("filter") and live_seen:
        raise BakeError(f"{where(anc)}: a filter above a mask or clip-path that sits above a baked layer "
                        "cannot be baked in the right order: move the filter into the layer")


def live_masks(root, layers):
    by_id = {el.get("id"): el for el in root.iter() if isinstance(el.tag, str) and el.get("id")}

    def target(holder, key, tag):
        found = re.fullmatch(r"\s*url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)\s*", holder.get(key))
        el = by_id.get(found.group(1)) if found else None
        if el is None or el.tag != q(tag):
            raise BakeError(f'{where(holder)}: {key}="{holder.get(key)}" does not point at a <{tag}>')
        return el

    masks = {}
    ancestors = {a for layer in layers.values() for a in live_ancestors(layer)}
    for holder in ancestors:
        if holder.get("clip-path") and target(holder, "clip-path", "clipPath").get("clipPathUnits") == "objectBoundingBox":
            raise BakeError(f"{where(holder)}: a clip-path above a baked layer needs clipPathUnits=\"userSpaceOnUse\"")
        if not holder.get("mask"):
            continue
        mask = target(holder, "mask", "mask")
        if (mask.get("maskUnits") != "userSpaceOnUse" or mask.get("maskContentUnits") == "objectBoundingBox"
                or any(mask.get(k) is None for k in ("x", "y", "width", "height"))):
            raise BakeError(f'{where(mask)}: mask #{mask.get("id")} sits above baked layers, so it needs '
                            'maskUnits="userSpaceOnUse" and x, y, width, height')
        space = ctm(holder) @ parse_transform(holder.get("transform"))
        ident = mask.get("id")
        if ident in masks and not np.allclose(masks[ident][1], space):
            raise BakeError(f"mask #{ident} is used above baked layers by groups with different transforms")
        masks.setdefault(ident, (mask, space, []))[2].append(holder)
    return masks


def prune(root, keep, drop):
    for st in list(root.iter(q("style"))):
        st.getparent().remove(st)

    def inside(el):
        for c in elements(el):
            if c in drop:
                el.remove(c)
            else:
                inside(c)

    for el in keep:
        inside(el)
    wanted = reachable_ids(root, keep)
    needed = {a for el in keep for a in el.iterancestors()}
    holding = etree.SubElement(root, q("defs"))

    def relocate(target, removed):
        context = {}
        for anc in target.iterancestors():
            for k in INHERITED:
                if anc.get(k) is not None and k not in context:
                    context[k] = anc.get(k)
        wrapper = etree.SubElement(holding, q("g"), {k: v for k, v in context.items() if target.get(k) is None})
        wrapper.append(target)

    def outside(el):
        for c in elements(el):
            if c in keep or c is holding:
                continue
            if c in needed:
                outside(c)
            elif local(c) not in NON_RENDERING:
                targets = [d for d in c.iter() if isinstance(d.tag, str) and d.get("id") in wanted]
                for d in targets:
                    if not any(a in targets for a in d.iterancestors()):
                        relocate(d, c)
                if c.getparent() is el:
                    el.remove(c)

    outside(root)


def same_in(doc, tree):
    return lambda el: doc.xpath(tree.getpath(el))[0]


def render_layer(tree, layer, work):
    doc = copy.deepcopy(tree)
    same = same_in(doc, tree)
    for anc in live_ancestors(layer):
        for k in LIVE_EFFECTS:
            same(anc).attrib.pop(k, None)
    keep = [same(el) for el in layer.elements]
    prune(doc.getroot(), keep, {same(el) for child in layer.children for el in child.elements})
    px = screenshot(etree.tostring(doc), canvas_size(doc.getroot()), work, f"layer-{layer.name}", RENDER_SCALE)
    if px.getbbox() is None:
        raise BakeError(f'layer "{layer.name}" rendered empty')
    return px


def render_mask(tree, mask, space, work):
    doc = copy.deepcopy(tree)
    root = doc.getroot()
    a, c, e = space[0]
    b, d, f = space[1]
    probe = etree.SubElement(root, q("g"), transform=f"matrix({a} {b} {c} {d} {e} {f})")
    etree.SubElement(probe, q("rect"), x=mask.get("x"), y=mask.get("y"), width=mask.get("width"),
                     height=mask.get("height"), fill="#fff", mask=f"url(#{mask.get('id')})")
    prune(root, [probe], set())
    return screenshot(etree.tostring(root), canvas_size(root), work, f"mask-{mask.get('id')}", RENDER_SCALE)


def encode_image(px, scale, quality):
    left, top, right, bottom = px.getbbox()
    s = RENDER_SCALE
    x0, y0, x1, y1 = left // s, top // s, -(-right // s), -(-bottom // s)
    crop = px.crop((x0 * s, y0 * s, x1 * s, y1 * s))
    if scale != 1:
        size = (max(1, round(crop.width * scale)), max(1, round(crop.height * scale)))
        crop = crop.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    buf = io.BytesIO()
    crop.save(buf, "WEBP", quality=quality, method=6, alpha_quality=100)
    img = etree.Element(q("image"))
    img.set("x", str(x0))
    img.set("y", str(y0))
    img.set("width", str(x1 - x0))
    img.set("height", str(y1 - y0))
    img.set("preserveAspectRatio", "none")
    img.set("href", "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode())
    return img


def place(img, parent_ctm):
    inv = np.linalg.inv(parent_ctm)
    if np.allclose(inv[:2, :2], np.identity(2)):
        img.set("x", f"{float(img.get('x')) + inv[0, 2]:.6g}")
        img.set("y", f"{float(img.get('y')) + inv[1, 2]:.6g}")
    else:
        a, c, e = inv[0]
        b, d, f = inv[1]
        img.set("transform", "matrix(" + " ".join(f"{v:.6g}" for v in (a, b, c, d, e, f)) + ")")


def build_wrapper(layer, images, parent_ctm):
    g = etree.Element(q("g"))
    ident = next((el.get("id") for el in layer.elements if el.get("id")), None)
    if ident:
        g.set("id", ident)
    if layer.first.get("class"):
        g.set("class", layer.first.get("class"))
    img = images[layer.name]
    place(img, parent_ctm)
    g.append(img)
    for child in layer.children:
        g.append(build_wrapper(child, images, parent_ctm))
    return g


def strip_effects(root, wrappers):
    inside_baked = {d for w in wrappers for d in w.iter()}
    by_id = {w.get("id"): w for w in inside_baked if isinstance(w.tag, str) and w.get("id")}
    _, rules = split_css(css_text(root))
    blend_classes = {c for selector, body in rules if "mix-blend-mode" in body for c in re.findall(r"\.([\w-]+)", selector)}

    def blends(el):
        return any(isinstance(d.tag, str) and ("mix-blend-mode" in (d.get("style") or "") or d.get("mix-blend-mode")
                   or set((d.get("class") or "").split()) & blend_classes) for d in el.iter())

    holders = {a for w in wrappers for a in w.iterancestors() if any(a.get(k) is not None for k in BAKED_EFFECTS)}
    for holder in holders:
        effects = [k for k in BAKED_EFFECTS if holder.get(k) is not None]
        if sum(holder in w.iterancestors() for w in wrappers) > 1:
            raise BakeError(f"{where(holder)}: {'/'.join(effects)} on a group with several baked layers would be applied "
                            "to each layer separately: mark the group itself, or move the effect down")
        stack = elements(holder)
        while stack:
            d = stack.pop()
            if d in inside_baked or local(d) in NON_RENDERING:
                continue
            target = by_id.get((d.get("href") or d.get(XLINK_HREF) or "").lstrip("#"))
            if local(d) in PAINTING and not (local(d) == "use" and target is not None and holder in target.iterancestors()):
                raise BakeError(f"{where(d)}: <{local(d)}> is not baked but sits under {', '.join(effects)} on {where(holder)}, "
                                "which is baked into the layers and removed: mark it with data-bake or move it out")
            stack.extend(elements(d))
        baked_here = {i for i, w in by_id.items() if holder in w.iterancestors()}
        for d in root.iter(q("use")):
            if holder not in d.iterancestors() and (d.get("href") or d.get(XLINK_HREF) or "").lstrip("#") in baked_here:
                raise BakeError(f"{where(d)}: <use> points at a layer that has {', '.join(effects)} from {where(holder)} "
                                "baked in, but the <use> sits outside that group: move the effect into the layer")
        for k in effects:
            del holder.attrib[k]
        if blends(holder):
            holder.set("style", ";".join(filter(None, [(holder.get("style") or "").rstrip(";"), "isolation:isolate"])))


def swap_masks(root, masks, rendered):
    taken = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    sizes = []
    for ident, (mask, space, holders) in masks.items():
        scale, quality = read_settings([mask], f"mask #{ident}", DEFAULT_MASK_SCALE)
        new_id = next(f"{ident}-baked{n or ''}" for n in range(len(taken) + 1) if f"{ident}-baked{n or ''}" not in taken)
        taken.add(new_id)
        live = etree.Element(q("mask"), id=new_id, maskUnits="userSpaceOnUse",
                             **{k: mask.get(k) for k in ("x", "y", "width", "height")})
        px = rendered[ident]
        if px.getbbox() is not None:
            img = encode_image(px, scale, quality)
            place(img, space)
            live.append(img)
            sizes.append((len(img.get("href")) * 3 // 4, f"mask #{ident}"))
        defs = root.find(q("defs"))
        if defs is None:
            defs = etree.Element(q("defs"))
            root.insert(0, defs)
        defs.append(live)
        for holder in holders:
            holder.set("mask", f"url(#{new_id})")
    return sizes


def drop_unused_defs(root):
    wanted = reachable_ids(root, [c for c in elements(root) if local(c) != "defs"] + list(root.iter(q("style"))))
    for defs in list(root.iter(q("defs"))):
        for d in elements(defs):
            ids = {x.get("id") for x in d.iter() if isinstance(x.tag, str) and x.get("id")}
            if ids and not ids & wanted:
                defs.remove(d)
        if not elements(defs):
            defs.getparent().remove(defs)


def check_output(root):
    if re.search(r"(^|[;{\s])filter\s*:", css_text(root)):
        raise BakeError("a CSS filter is left in the <style>: filters must sit inside a data-bake layer")
    ids = {el.get("id") for el in root.iter() if isinstance(el.tag, str) and el.get("id")}
    used = {r for el in root.iter() if isinstance(el.tag, str) for r in references(el)}
    missing = sorted(used - ids)
    if missing:
        raise BakeError(f"the baked SVG references ids that no longer exist: {', '.join('#' + m for m in missing)}; "
                        "an id inside a layer only survives on the layer element itself")
    for el in root.iter():
        if isinstance(el.tag, str) and (el.tag == q("filter") or el.get("filter")
                                        or re.search(r"(^|;)\s*filter\s*:", el.get("style") or "")):
            label = f'<{local(el)}{" id=" + el.get("id") if el.get("id") else ""}>'
            raise BakeError(f"{label} still has a filter after baking: filters must sit inside a data-bake layer")


def bake(src, work, jobs):
    tree = etree.parse(str(src), etree.XMLParser(huge_tree=True, remove_blank_text=True))
    root = tree.getroot()
    canvas_size(root)
    layers = collect_layers(root)
    if not layers:
        raise BakeError("no element has data-bake")
    masks = live_masks(root, layers)
    check_rules(root, layers, masks)

    jobs_list = [("layer", layer) for layer in layers.values()] + [("mask", m) for m in masks.values()]

    def run(job):
        kind, item = job
        return render_layer(tree, item, work) if kind == "layer" else render_mask(tree, item[0], item[1], work)

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(run, jobs_list))
    layer_px = dict(zip(layers, results[:len(layers)]))
    mask_px = dict(zip(masks, results[len(layers):]))
    images = {name: encode_image(px, layers[name].scale, layers[name].quality) for name, px in layer_px.items()}

    wrappers = []
    for layer in layers.values():
        if layer.parent is not None:
            continue
        wrapper = build_wrapper(layer, images, ctm(layer.first))
        layer.first.getparent().replace(layer.first, wrapper)
        for el in layer.elements[1:]:
            el.getparent().remove(el)
        wrappers.append(wrapper)

    strip_effects(root, wrappers)
    sizes = [(len(img.get("href")) * 3 // 4, name) for name, img in images.items()]
    sizes += swap_masks(root, masks, mask_px)
    drop_unused_defs(root)
    check_output(root)
    for el in root.iter():
        if isinstance(el.tag, str):
            for k in [k for k in el.attrib if k.startswith("data-bake")]:
                del el.attrib[k]
    etree.indent(root, space="  ")
    return etree.tostring(root, encoding="utf-8") + b"\n", sorted(sizes, reverse=True)


def with_css(svg_bytes, css):
    out, n = re.subn(rb"</svg>\s*$", f"<style>{css}</style></svg>".encode(), svg_bytes)
    if n != 1:
        raise BakeError("the SVG must end with </svg>")
    return out


def worst_tile(a, b):
    diff = ((a - b) ** 2).mean(axis=2)
    worst, at = 0.0, (0, 0)
    for y in range(0, diff.shape[0], CHECK_TILE):
        for x in range(0, diff.shape[1], CHECK_TILE):
            mse = diff[y:y + CHECK_TILE, x:x + CHECK_TILE].mean()
            if mse > worst:
                worst, at = mse, (x, y)
    return (math.inf if worst == 0 else 10 * math.log10(255 ** 2 / worst)), at


def check(src_bytes, out_bytes, work, jobs):
    shutil.rmtree(CHECK_DIR, ignore_errors=True)
    CHECK_DIR.mkdir(parents=True)
    root = etree.fromstring(src_bytes, etree.XMLParser(huge_tree=True))
    size = canvas_size(root)
    frames = [("static", bg, "*{animation:none!important}") for bg in CHECK_BACKGROUNDS]
    frames += [(f"{t:.3f}s", CHECK_BACKGROUNDS[0], f"*{{animation-delay:-{t}s!important;animation-play-state:paused!important}}")
               for t in check_times(css_text(root))]
    labels = [f"{name}-{bg.lstrip('#')}" for name, bg, _ in frames]
    shots = [(f"{kind}-{label}", with_css(data, css), bg)
             for label, (_, bg, css) in zip(labels, frames) for kind, data in (("source", src_bytes), ("baked", out_bytes))]
    with ThreadPoolExecutor(jobs) as pool:
        px = dict(zip((s[0] for s in shots),
                      pool.map(lambda s: screenshot(s[1], size, work, s[0], 1, s[2]).convert("RGB"), shots)))
    failed, worst = [], (math.inf, "")
    for label in labels:
        a, b = px[f"source-{label}"], px[f"baked-{label}"]
        psnr, (x, y) = worst_tile(np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64))
        side = Image.new("RGB", (size[0] * 2, size[1]))
        side.paste(a, (0, 0))
        side.paste(b, (size[0], 0))
        side.save(CHECK_DIR / f"{label}.png")
        worst = min(worst, (psnr, f"{label} at x={x} y={y}"))
        if psnr < CHECK_MIN_TILE_PSNR:
            failed.append(f"{label} (x={x} y={y}: {psnr:.1f} dB)")
    print(f"check: {len(labels)} frames compared, worst tile {worst[0]:.1f} dB in {worst[1]} "
          f"(limit {CHECK_MIN_TILE_PSNR:g}); source left, baked right in {CHECK_DIR}")
    if failed:
        more = f" and {len(failed) - 5} more" if len(failed) > 5 else ""
        raise BakeError("baked output differs from the source at " + ", ".join(failed[:5]) + more)


def main():
    ap = argparse.ArgumentParser(description="Bake the data-bake layers of an animated SVG into embedded WebP images.")
    ap.add_argument("src", nargs="?", type=Path, default=ROOT / "src/profile-wall.svg")
    ap.add_argument("out", nargs="?", type=Path, default=ROOT / "assets/profile-wall.svg")
    ap.add_argument("--skip-check", action="store_true", help="write the output without comparing it to the source")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()
    src, out = args.src.resolve(), args.out.resolve()
    try:
        if src == out:
            raise BakeError("source and output are the same file")
        if not src.is_file():
            raise BakeError(f"{src} does not exist")
        if not out.parent.is_dir():
            raise BakeError(f"{out.parent} does not exist")
        if args.jobs < 1:
            raise BakeError("--jobs must be at least 1")
        if not features.check("webp"):
            raise BakeError("this Pillow build has no WebP support")
        TMP.mkdir(exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="bake-work-", dir=TMP))
        try:
            data, sizes = bake(src, work, args.jobs)
            if not args.skip_check:
                check(src.read_bytes(), data, work, args.jobs)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        out.write_bytes(data)
        print(f"{out}: {len(data) / 1024:.0f} KB; " + ", ".join(f"{n} {s // 1024}K" for s, n in sizes))
    except BakeError as e:
        sys.exit(f"bake: {e}")


if __name__ == "__main__":
    main()
