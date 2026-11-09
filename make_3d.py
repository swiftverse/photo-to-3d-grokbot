#!/usr/bin/env python3
"""Turn one photo (a car, a street, a room) into a 2.5D 3D scene.

photo -> monocular depth (Depth Anything V2 Small, CPU) -> textured grid mesh (.glb)
      -> self-contained three.js viewer (.html) -> orbit video (.mp4) + loop (.gif)

Usage:
    python make_3d.py photo.jpg                # everything, written to ./out/<photo-name>/
    python make_3d.py photo.jpg --no-video     # skip the headless-Chrome render
"""
import argparse, base64, io, json, os, shutil, subprocess, sys, time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
MODEL_ID = "depth-anything/Depth-Anything-V2-Small-hf"
TIMINGS = {}


class step:
    """Context manager that prints and records how long each stage takes."""
    def __init__(self, name): self.name = name
    def __enter__(self):
        print(f"[{self.name}] ...", flush=True); self.t0 = time.perf_counter(); return self
    def __exit__(self, *exc):
        dt = time.perf_counter() - self.t0; TIMINGS[self.name] = round(dt, 2)
        print(f"[{self.name}] done in {dt:.1f}s", flush=True)


# ---------------------------------------------------------------- 1. depth
def estimate_depth(img: Image.Image) -> np.ndarray:
    """Return relative inverse depth (bigger = closer), same size as img, float32."""
    import torch
    from transformers import pipeline
    torch.set_num_threads(os.cpu_count() or 4)
    pipe = pipeline("depth-estimation", model=MODEL_ID, device="cpu")
    with torch.inference_mode():
        out = pipe(img)
    pred = out["predicted_depth"]
    if pred.ndim == 2: pred = pred[None, None]
    elif pred.ndim == 3: pred = pred[None]
    pred = torch.nn.functional.interpolate(pred.float(), size=(img.height, img.width),
                                           mode="bicubic", align_corners=False)
    return pred[0, 0].numpy().astype(np.float32)


def save_depth_images(disp, img, out):
    import cv2
    lo, hi = np.percentile(disp, [1, 99])
    n = np.clip((disp - lo) / (hi - lo + 1e-6), 0, 1)
    color = cv2.applyColorMap((n * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)[:, :, ::-1]
    Image.fromarray(color).save(out / "depth.png")
    side = Image.new("RGB", (img.width * 2, img.height))
    side.paste(img, (0, 0)); side.paste(Image.fromarray(color), (img.width, 0))
    side.save(out / "photo_vs_depth.jpg", quality=88)


# ---------------------------------------------------------------- 2. mesh
def build_mesh(disp, img, grid_w, near, far, hfov_deg, edge_ratio):
    """Back-project a downsampled depth grid through a pinhole camera into a mesh.

    The camera sits at the origin looking down -Z (three.js convention), so the original
    photo is reproduced exactly from (0,0,0) and the 3D only shows when the camera moves.
    Inverse depth from the model is affine-ambiguous, so it is mapped to near..far.
    """
    import cv2
    W, H = img.size
    gw = grid_w; gh = int(round(H * gw / W))
    d = cv2.resize(disp, (gw, gh), interpolation=cv2.INTER_AREA)
    d = cv2.medianBlur(d, 3)                                   # kill single-pixel spikes
    lo, hi = np.percentile(d, [1, 99.5])
    dn = np.clip((d - lo) / (hi - lo + 1e-6), 0, 1).astype(np.float32)
    verts, faces, uvs, z = grid_to_mesh(dn, near, far, hfov_deg)
    # drop "rubber sheet" triangles that bridge a depth discontinuity (object edges)
    zf = z.reshape(-1)[faces]
    keep = zf.max(1) / zf.min(1) < edge_ratio
    stats = dict(grid=f"{gw}x{gh}", vertices=int(len(verts)), faces_total=int(len(faces)),
                 faces_kept=int(keep.sum()), faces_trimmed=int((~keep).sum()),
                 trimmed_pct=round(100 * float((~keep).mean()), 2),
                 depth_range=[near, far], median_depth=round(float(np.median(z)), 3))
    return verts, faces[keep], uvs, dn, stats


def build_back_layer(disp_grid_n, img, hole_kernel, hole_width, hole_thresh):
    """Background layer that fills the holes left behind foreground objects.

    Depth: a wide, short min-filter on inverse depth pushes every occluder back to the depth
    of the scene on either side of it. The kernel is wide because the camera mostly moves
    sideways, and a big subject like a car needs background from beyond its own width.
    Texture: the occluder pixels are inpainted so the fill is not a ghost copy of the object.
    """
    import cv2
    gh, gw = disp_grid_n.shape
    kw = max(3, int(hole_width * gw) | 1)
    bg = cv2.erode(disp_grid_n, np.ones((hole_kernel, kw), np.uint8))
    bg = cv2.GaussianBlur(bg, (0, 0), sigmaX=hole_kernel, sigmaY=hole_kernel / 2)
    bg = np.minimum(bg, disp_grid_n)
    occl = (disp_grid_n - bg) > hole_thresh                     # pixels that sit in front of something
    W, H = img.size
    m = cv2.resize(occl.astype(np.uint8) * 255, (W, H), interpolation=cv2.INTER_NEAREST)
    m = cv2.dilate(m, np.ones((19, 19), np.uint8)) > 0       # include soft object borders
    rgb = np.asarray(img)
    out = mirror_fill(rgb, m)
    return bg, Image.fromarray(np.ascontiguousarray(out)), float(occl.mean())


def mirror_fill(rgb, mask):
    """Fill masked pixels row by row by mirroring the background on either side of the gap.

    The camera mostly moves sideways, so what gets revealed is a strip just left or right of
    an object. Mirroring the neighbouring background keeps its texture (grass stays grass)
    instead of the radial smear a diffusion inpainter leaves in large holes. The two mirrored
    copies are cross-faded across the gap, then the fill is softened slightly.
    """
    import cv2
    H, W, _ = rgb.shape
    src = rgb.astype(np.float32)
    out = src.copy()
    for y in range(H):
        row = mask[y]
        if not row.any(): continue
        d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        for x0, x1 in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):   # gap is [x0, x1)
            xs = np.arange(x0, x1)
            n = x1 - x0
            left = np.clip(2 * x0 - 1 - xs, 0, W - 1)                          # mirror around x0
            right = np.clip(2 * x1 - xs, 0, W - 1)                             # mirror around x1
            wl = (x1 - xs) / (n + 1.0) if x0 > 0 else np.zeros(n)
            wr = (xs - x0 + 1) / (n + 1.0) if x1 < W else np.zeros(n)
            if x0 == 0 and x1 == W: continue
            wsum = wl + wr
            wl, wr = wl / wsum, wr / wsum
            out[y, x0:x1] = src[y, left] * wl[:, None] + src[y, right] * wr[:, None]
    soft = cv2.GaussianBlur(out, (0, 0), 1.5)
    out = np.where(mask[:, :, None], soft, src)
    return np.clip(out, 0, 255).astype(np.uint8)


def pad_layer(dn, tex, frac):
    """Extend a depth grid and its texture past the photo border (mirrored) so that orbiting
    the camera shows more scene instead of the empty edge of the world."""
    import cv2
    gh, gw = dn.shape
    p = int(round(frac * gw))
    if p == 0: return dn, tex, 0
    dn_p = cv2.copyMakeBorder(dn, p, p, p, p, cv2.BORDER_REPLICATE)
    W, H = tex.size
    tp = int(round(p * W / gw))
    t = cv2.copyMakeBorder(np.asarray(tex), tp, tp, tp, tp, cv2.BORDER_REFLECT)
    return dn_p, Image.fromarray(t), p


def grid_to_mesh(dn, near, far, hfov_deg, push=1.0, pad=0):
    """Back-project an inverse-depth grid. `pad` cells on each side lie outside the photo."""
    gh, gw = dn.shape
    iw, ih = gw - 2 * pad, gh - 2 * pad                         # size of the photo area in cells
    z = push / (dn * (1.0 / near - 1.0 / far) + 1.0 / far)
    f = (iw / 2) / np.tan(np.radians(hfov_deg) / 2)
    u, v = np.meshgrid(np.arange(gw) - pad + 0.5, np.arange(gh) - pad + 0.5)
    X = (u - iw / 2) / f * z
    Y = -(v - ih / 2) / f * z
    verts = np.stack([X, Y, -z], -1).reshape(-1, 3).astype(np.float32)
    uvs = np.stack([(u + pad) / gw, 1 - (v + pad) / gh], -1).reshape(-1, 2).astype(np.float32)
    idx = np.arange(gw * gh).reshape(gh, gw)
    a, b, c, e = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    faces = np.concatenate([np.stack([a, c, b], -1).reshape(-1, 3),
                            np.stack([b, c, e], -1).reshape(-1, 3)])
    return verts, faces, uvs, z


def textured(verts, faces, uvs, tex_img, tex_w=1280, name="mesh"):
    import trimesh
    tex = tex_img if tex_img.width <= tex_w else tex_img.resize((tex_w, int(tex_img.height * tex_w / tex_img.width)), Image.LANCZOS)
    buf = io.BytesIO(); tex.save(buf, format="JPEG", quality=88); buf.seek(0)
    tex = Image.open(buf)                      # keep JPEG format so the .glb embeds a JPEG, not a PNG
    mat = trimesh.visual.material.PBRMaterial(name=name, baseColorTexture=tex, metallicFactor=0.0,
                                              roughnessFactor=1.0, doubleSided=True)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces.astype(np.uint32), process=False,
                           visual=trimesh.visual.TextureVisuals(uv=uvs, material=mat))
    mesh.remove_unreferenced_vertices()
    return mesh


def export_glb(meshes, path):
    import trimesh
    scene = trimesh.Scene()
    for name, m in meshes: scene.add_geometry(m, geom_name=name, node_name=name)
    path.write_bytes(trimesh.exchange.gltf.export_glb(scene))


# ---------------------------------------------------------------- 3. viewer
def write_viewer(glb_path, out_html, img, cfg, title, credit):
    top = np.asarray(img.resize((64, 64)))[:6].reshape(-1, 3).mean(0)
    bg = "#%02x%02x%02x" % tuple(int(x) for x in top)
    cfg = dict(cfg, bg=bg)
    html = (HERE / "web" / "viewer_template.html").read_text()
    html = (html.replace("__TITLE__", title).replace("__BG__", bg).replace("__CREDIT__", credit)
                .replace("__CONFIG__", json.dumps(cfg))
                .replace("__THREE_BUNDLE__", (HERE / "web" / "three-bundle.min.js").read_text())
                .replace("__GLB_B64__", base64.b64encode(glb_path.read_bytes()).decode()))
    out_html.write_text(html)


# ---------------------------------------------------------------- 4. video
def find_chrome():
    for p in [os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("chromium"),
              shutil.which("chromium-browser"),
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"]:
        if p and Path(p).exists(): return p
    return None


def render_video(html_path, out_dir, frames, fps, size, gif_w):
    from playwright.sync_api import sync_playwright
    import imageio.v2 as imageio
    chrome = find_chrome()
    w, h = size
    mp4 = out_dir / "orbit.mp4"
    with sync_playwright() as p:
        kw = dict(executable_path=chrome) if chrome else {}
        browser = p.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                                                         "--ignore-gpu-blocklist"], **kw)
        page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
        page.goto(html_path.resolve().as_uri())
        page.wait_for_function("window.__ready === true", timeout=120_000)
        page.evaluate("window.__scripted = true; window.hideHud();")
        writer = imageio.get_writer(mp4, fps=fps, codec="libx264", quality=None,
                                    pixelformat="yuv420p", ffmpeg_params=["-crf", "18", "-preset", "slow",
                                                   "-movflags", "+faststart"])
        for i in range(frames):
            data_url = page.evaluate(f"window.setView({i / frames}); "
                                     "document.querySelector('canvas').toDataURL('image/png')")
            frame = Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))).convert("RGB")
            writer.append_data(np.asarray(frame))
            if i == 0 or i == frames // 4 or i == frames // 2 or i == 3 * frames // 4:
                frame.save(out_dir / f"frame_{i:03d}.jpg", quality=90)
        writer.close(); browser.close()
    # GIF: half frame-rate, palette per clip, loops forever
    ff = shutil.which("ffmpeg")
    if not ff:
        import imageio_ffmpeg; ff = imageio_ffmpeg.get_ffmpeg_exe()
    gif = out_dir / "orbit.gif"
    vf = f"fps={fps // 2},scale={gif_w}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=160[p];[b][p]paletteuse=dither=sierra2_4a"
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(mp4), "-vf", vf, "-loop", "0", str(gif)], check=True)
    return mp4, gif


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("photo")
    ap.add_argument("--out", default=None, help="output folder (default ./out/<photo stem>)")
    ap.add_argument("--max-size", type=int, default=1280, help="resize photo so its long side is at most this")
    ap.add_argument("--grid", type=int, default=512, help="mesh grid width in vertices")
    ap.add_argument("--near", type=float, default=1.0)
    ap.add_argument("--far", type=float, default=9.0, help="far/near ratio sets how deep the scene feels")
    ap.add_argument("--fov", type=float, default=60.0, help="assumed horizontal field of view of the photo")
    ap.add_argument("--edge-ratio", type=float, default=1.05, help="trim triangles whose depth ratio exceeds this")
    ap.add_argument("--frames", type=int, default=150)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--video-size", default="1280x720")
    ap.add_argument("--gif-width", type=int, default=560)
    ap.add_argument("--yaw", type=float, default=6.0, help="orbit swing in degrees for the video")
    ap.add_argument("--pivot", type=float, default=0.95, help="orbit pivot as a fraction of the median depth")
    ap.add_argument("--margin", type=float, default=0.97, help="how much of the photo the camera frames (1.0 = all of it)")
    ap.add_argument("--dolly", type=float, default=0.04, help="how far the video camera pushes in, fraction of the pivot distance")
    ap.add_argument("--credit", default="", help="photo credit shown in the viewer")
    ap.add_argument("--no-fill", action="store_true", help="skip the inpainted background layer")
    ap.add_argument("--hole-kernel", type=int, default=9, help="grid px; vertical reach of the fill min-filter")
    ap.add_argument("--hole-width", type=float, default=0.8, help="horizontal reach of the fill min-filter, fraction of width")
    ap.add_argument("--pad", type=float, default=0.2, help="extend the fill layer past the photo edges by this fraction")
    ap.add_argument("--hole-thresh", type=float, default=0.06)
    ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args()

    photo = Path(a.photo)
    out = Path(a.out) if a.out else Path("out") / photo.stem
    out.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()

    with step("load"):
        img = Image.open(photo).convert("RGB")
        if max(img.size) > a.max_size:
            s = a.max_size / max(img.size)
            img = img.resize((round(img.width * s), round(img.height * s)), Image.LANCZOS)
        print(f"  photo {img.width}x{img.height}")

    with step("depth"):
        disp = estimate_depth(img)
        np.save(out / "depth_raw.npy", disp)
        save_depth_images(disp, img, out)

    with step("mesh"):
        verts, faces, uvs, dn, stats = build_mesh(disp, img, a.grid, a.near, a.far, a.fov, a.edge_ratio)
        meshes = [("front", textured(verts, faces, uvs, img, name="front"))]
        if not a.no_fill:
            bg_dn, bg_img, occl_frac = build_back_layer(dn, img, a.hole_kernel, a.hole_width, a.hole_thresh)
            bg_img.save(out / "background_inpainted.jpg", quality=88)
            import cv2
            bg_small = cv2.resize(bg_dn, (bg_dn.shape[1] // 2, bg_dn.shape[0] // 2), interpolation=cv2.INTER_AREA)
            bg_small, bg_tex, pad = pad_layer(bg_small, bg_img, a.pad)    # half-res is plenty for a blurry fill
            bv, bf, buv, _ = grid_to_mesh(bg_small, a.near, a.far, a.fov, push=1.03, pad=pad)
            meshes.append(("fill", textured(bv, bf, buv, bg_tex, tex_w=1600, name="fill")))
            stats.update(occluder_pct=round(100 * occl_frac, 2))
        export_glb(meshes, out / "scene.glb")
        stats.update(glb_vertices=int(sum(len(m.vertices) for _, m in meshes)),
                     glb_faces=int(sum(len(m.faces) for _, m in meshes)))
        print("  " + json.dumps(stats))

    hfov = np.radians(a.fov)
    vfov_img = 2 * np.arctan(np.tan(hfov / 2) * img.height / img.width)
    tdepth = float(a.pivot * stats["median_depth"])       # orbit pivot between foreground and middle distance
    cfg = dict(hfovImage=float(hfov), vfovImage=float(vfov_img), vfov=float(np.degrees(vfov_img)),
               margin=a.margin, targetDepth=tdepth, maxYaw=float(np.radians(10)), maxPitch=float(np.radians(8)),
               pathYaw=float(np.radians(a.yaw)), pathPitch=float(np.radians(a.yaw * 0.2)), pathDolly=a.dolly)
    with step("viewer"):
        write_viewer(out / "scene.glb", out / "viewer.html", img, cfg, f"{photo.stem} in 3D", a.credit)

    if not a.no_video:
        with step("video"):
            w, h = map(int, a.video_size.split("x"))
            render_video(out / "viewer.html", out, a.frames, a.fps, (w, h), a.gif_width)

    TIMINGS["total"] = round(time.perf_counter() - t_all, 2)
    sizes = {p.name: p.stat().st_size for p in sorted(out.iterdir()) if p.is_file()}
    report = dict(photo=str(photo), size=list(img.size), model=MODEL_ID, mesh=stats, timings_s=TIMINGS, bytes=sizes)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
