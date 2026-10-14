"""texbind.py — which TXBP does a mesh's texture id refer to?

Texture ids are unique within a stage/character file but a pack usually
holds only the textures of its sibling mesh (id 0 is "the one texture in
this pack" and recurs dozens of times in a stage ARC). Meshes and packs sit
in parallel containers: mesh `/0/7/19/N` ↔ pack `/0/7/20/N`,
`/0/4/0/0` ↔ `/0/4/1/0`. So for every id we pick, among the packs that
contain it, the one whose container path is closest to the mesh's path
(longest common prefix, then same trailing index, then nearest sibling).
"""
from __future__ import annotations

from typing import Dict, List, Tuple


def _segs(p: str) -> List[int]:
    return [int(s) for s in p.strip("/").split("/") if s != ""]


def _score(mesh: List[int], pack: List[int]) -> Tuple[int, int, int]:
    common = 0
    for a, b in zip(mesh, pack):
        if a != b:
            break
        common += 1
    same_last = int(mesh[-1:] == pack[-1:])
    diff = abs(mesh[common] - pack[common]) if common < min(len(mesh), len(pack)) else 0
    return (common, same_last, -diff)


def bind(mesh_path: str, packs: List[Tuple[str, Dict[int, str]]], slots) -> Dict[int, str]:
    """{texture id: png name} for the given mesh, best pack per id."""
    m = _segs(mesh_path)
    out: Dict[int, str] = {}
    for tid in slots:
        if tid < 0 or tid >= 0x80000000:
            continue
        best = None
        for ppath, ids in packs:
            if tid in ids:
                sc = _score(m, _segs(ppath))
                if best is None or sc > best[0]:
                    best = (sc, ids[tid])
        if best:
            out[tid] = best[1]
    return out


def collect_packs(leaf_iter, inflate, parse_txbp, png_name):
    """Helper shared by browse/render: returns (packs, meshes_raw)."""
    packs, meshes = [], []
    for path, leaf in leaf_iter:
        try:
            data = inflate(leaf)
        except Exception:
            continue
        if data[:4] == b"TXBP":
            try:
                ids = {t.tex_id: png_name(path, t.index) for t in parse_txbp(data)}
            except Exception:
                continue
            packs.append((path, ids))
        elif data[:4] == b"OME\0":
            meshes.append((path, data))
    return packs, meshes


def png_name(path: str, index: int) -> str:
    return f"{path.strip('/').replace('/', '-') or 'tex'}_{index}.png"


def bind_file(packs: List[Tuple[str, Dict[int, str]]], mesh_slots: Dict[str, set]) -> Dict[str, Dict[int, str]]:
    """Bind every mesh in a file at once.

    Character files (players, enemies, NPCs) use texture id 0 everywhere, so
    the id alone says nothing. Observed layout: mesh container index k pairs
    with texture-pack index k when such a pack exists; a mesh with no
    same-index pack takes the nearest pack that no mesh claimed exactly
    (E333: meshes {0, 9} ↔ packs {0, 5} — the 110-vertex decal mesh 9 is the
    dragon tattoo overlay in pack 5); failing that, the nearest pack. Stage
    ARCs still resolve through :func:`bind` because their ids are real.
    """
    out: Dict[str, Dict[int, str]] = {}
    packs_by_last = {}
    for ppath, ids in packs:
        packs_by_last.setdefault(_segs(ppath)[-1] if _segs(ppath) else -1, []).append((ppath, ids))
    claimed = set()
    for mpath in mesh_slots:
        k = _segs(mpath)[-1] if _segs(mpath) else -1
        if k in packs_by_last:
            claimed.add(packs_by_last[k][0][0])
    for mpath, slots in mesh_slots.items():
        slots = {s for s in slots if 0 <= s < 0x80000000} or {0}
        res = bind(mpath, packs, slots)
        # ids were real and resolved → keep (stage archives)
        if res and any(s != 0 for s in slots):
            out[mpath] = res
            continue
        k = _segs(mpath)[-1] if _segs(mpath) else -1
        m = _segs(mpath)
        cand = None
        if k in packs_by_last:
            cand = packs_by_last[k][0]
        else:
            pool = [(p, ids) for p, ids in packs if p not in claimed] or packs
            if pool:        # nearest unclaimed pack, else nearest pack (player heads → face)
                cand = min(pool, key=lambda pi: (abs((_segs(pi[0])[-1] if _segs(pi[0]) else 0) - k),
                                                 -_score(m, _segs(pi[0]))[0]))
        if cand and cand[1]:
            png = cand[1].get(0, next(iter(cand[1].values())))
            out[mpath] = {s: png for s in (slots | {-1})}
        else:
            out[mpath] = res
    return out
