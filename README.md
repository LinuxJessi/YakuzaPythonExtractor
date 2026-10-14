# Yakuza (PS2) Python Extractor

An extract → browse → **mod** kit for **Yakuza (USA, SLUS-21348)** on
PlayStation 2, written in pure Python with **no required dependencies**.
Point it at your own legally obtained disc image and it will:

* **unpack** all 15,612 files (4.3 GB) hidden inside the four CRI
  `OGRE*.CVM` volumes — without needing the CVM password, because the game
  ships its own directory index;
* **convert** them to formats your desktop opens: textures → PNG (83k),
  models → OBJ + MTL (37k meshes, textures bound), characters → Blender,
  voice and music → WAV, sound effects → WAV, movies → MP4, plus rendered
  preview images of every mesh;
* **write mods back** into a bootable ISO: swap any file, or import a PNG
  straight into a texture slot.

This tool ships no game data. It is the companion to the
[Xenosaga I](https://github.com/LinuxJessi/Xenosaga1PythonExtractor) and
[Xenosaga III](https://github.com/LinuxJessi/Xenosaga3PythonExtractor)
extractors — same spirit, same GUI, a completely different engine. Every
format here was reversed from the disc and the game's executable; the
write-up is in [docs/FORMATS.md](docs/FORMATS.md).

---

## Quick start

### The GUI (recommended)

| OS | Double-click | or from a terminal |
|---|---|---|
| Windows | `launch.bat` | `py gui.py` |
| macOS | `launch.command` (first time: right-click → Open) | `python3 gui.py` |
| Linux | `launch.sh` | `python3 gui.py` |

A local web page opens with one card per command — **list / extract /
census / textures / models / previews / Blender / voice+music / sound
effects / movies / import texture / replace file / verify** — each with
file pickers and a live log. It auto-detects your ISO and shows which
optional tools it found. Everything the GUI does shells out to the scripts
below, so GUI and command line can never disagree.

### The command line

```sh
python3 cli.py list    "Yakuza (USA).iso"                 # every file: size, LBA, path
python3 cli.py extract "Yakuza (USA).iso" --out dump      # unpack, in-game tree
python3 cli.py census  "Yakuza (USA).iso"                 # extension × magic table

python3 browse.py textures dump out/textures              # TXBP packs → PNG
python3 browse.py models   dump out/models                # OME meshes → OBJ + MTL
python3 render.py dump out/textures out/previews          # textured preview PNGs (numpy + Pillow)
python3 blender_open.py out/models E333                   # a character in Blender

python3 media.py audio  dump out/audio                    # AHX voice + ADX music → WAV (ffmpeg)
python3 media.py sfx    dump out/sfx                      # ps2_DTPK banks → WAV
python3 media.py movies dump out/movies                   # SFD → MP4 (ffmpeg)

python3 repack.py texture "Yakuza (USA).iso" --file /MEDIA/PLAYER/PLAYER0.DAT \
                          --leaf /0/0 --index 0 --png my_suit.png --out modded.iso
python3 repack.py patch   "Yakuza (USA).iso" --file /MEDIA/MAP/MAP_ADV.DAT --with edited.dat --out modded.iso
python3 repack.py verify  modded.iso
```

Every converter takes `--filter SUBSTRING` to limit itself to matching
in-game paths (`--filter MEDIA/PLAYER`, `--filter E230`) and `--jobs N`
for parallelism.

### Requirements

* Python 3.9+ — unpacking, textures, models and repacking need nothing else.
* Optional: `numpy` + `Pillow` for rendered previews and PNG texture
  import; `ffmpeg` on PATH for voice/music/movies; Blender for
  `blender_open.py`; the Xenosaga III kit's `dap_decode.py` (found
  automatically if the two kits sit side by side, or copy it in) for sound
  effects.

---

## What's on the disc

| In-game path | Contents | Converted by |
|---|---|---|
| `MEDIA/PLAYER/*.DAT` | Kiryu — adventure / battle / event outfits, prison uniform (`*B.DAT`) | textures, models |
| `MEDIA3/ENEMY/Exxx/` | every enemy, NPC and story character (463 sets: `E…00` body, `E…01` head, 02–05 LODs) — Majima is **E230/E231**, shirtless Kiryu with the dragon tattoo is **E333** | textures, models, Blender |
| `MEDIA3/ENEMY/ENECOMMON.DAT` | the shared hands (meshes 13/14 + six poses) every enemy borrows | models, Blender |
| `MEDIA3/ARC/*.ARC` | Kamurocho street blocks by time of day / weather (`DAY_`, `NIGHT_`, `RAIN_`, `OLD_`, `ST_`) | textures, models, previews |
| `MEDIA/STAGE/`, `MEDIA/INDIVIDUALOBJ/`, `MEDIA/WANDERER/`, `MEDIA/PEOPLE/` | interiors, props, street NPCs | textures, models |
| `MEDIA/PAUSEMENU/TEX/` | item icons | textures |
| `MEDIA/TALK/PICTURE/*.SGT` | talk portraits (64-tile sheets) | textures |
| `MEDIA/SE/`, `MEDIA/SESET/` | sound-effect banks (`ps2_DTPK`) | sfx |
| `MEDIA2/SOUND/*.AHX` | 4,813 voice clips, named by speaker and scene | audio |
| `MEDIA2/BGM/*.ADX` | 132 music tracks | audio |
| `MEDIA2/MOVIE/*.SFD` | 41 FMV cutscenes | movies |
| `MEDIA2/AUTHOR/` | real-time cutscene streams (`.B##`) — not yet decoded | — |
| `MEDIA/TALK/**/TALK_*.BIN` | dialogue / message text — glyph-indexed, not yet decoded | — |
| `MEDIA4/MODULE/*.BIN` | minigame code overlays (Metrowerks `MWo3`) | — |

Output mirrors the in-game path: `MEDIA/PLAYER/PLAYER0.DAT` yields
`out/textures/MEDIA/PLAYER/PLAYER0.DAT/0-0_0.png` (container leaf `/0/0`,
texture 0) and `out/models/MEDIA/PLAYER/PLAYER0.DAT/1-0.obj` + `.mtl` +
`.json`.

---

## Use cases

### 3D models

`browse.py models` writes every mesh as OBJ with UVs and normals, one
group per sub-mesh, and an MTL whose materials point at the sibling PNGs.
Open any `.obj` with macOS Quick Look or Blender and the textures come
along. The accompanying `.json` sidecar records bone-derived attachment
points.

Characters are split across files (body, head, and shared hands), so use
`blender_open.py` to assemble one:

```sh
python3 blender_open.py out/models E230     # Majima
python3 blender_open.py out/models E333     # shirtless Kiryu, tattoo included
python3 blender_open.py out/models MEDIA/PLAYER/BATTLE.DAT
python3 blender_open.py out/models MEDIA3/ARC/DAY_KAB0000.ARC   # a whole street block
```

It launches Blender with the parts imported, textures bound, the shared
hands snapped to the wrists and given the body's skin material. Known
limits: hands keep the generic rest-pose angle, and player head meshes are
single-material where the game layers face + hair sheets, so one of the two
is approximated. Skeletons (`OMZ`) and motion (`.OMT`) are not exported
yet, so meshes come out in the rest pose, unrigged.

Want to just look? `render.py` draws a textured, lit preview PNG of every
mesh and a composite per file, so you can flip through 37k meshes in an
image viewer.

### Texture mods

```sh
python3 browse.py textures dump out/textures --filter MEDIA/PLAYER/PLAYER0.DAT
# edit out/textures/MEDIA/PLAYER/PLAYER0.DAT/0-0_0.png (keep the size)
python3 repack.py texture "Yakuza (USA).iso" --file /MEDIA/PLAYER/PLAYER0.DAT \
        --leaf /0/0 --index 0 --png edited.png --out modded.iso
python3 repack.py verify modded.iso
```

The PNG is quantised to the slot's original palette size (16 or 256
colours; RGBA32 slots take the pixels as-is), re-encoded, recompressed with
the engine's own LZSS, written back into the container with every offset
preserved, and the disc index updated. The only constraint is that the new
texture must compress to no more than the original — flat or painterly
edits always do; heavy photo detail may not, and the tool tells you.
Verified with a round trip: a red-tinted suit imported into a copy of the
disc, re-extracted, and converted comes back red (through the palette
quantisation), the index verifies, and neighbouring files are untouched.
Not yet confirmed in-game on hardware or PCSX2 — if you try it, say so in
an issue.

### Replacing any file (translation and other data mods)

`repack.py patch` writes an edited file over its original extent and fixes
the size in the `OGREDIR` index. Works for anything on the disc as long as
the replacement fits in the sectors the original occupied (same count or
fewer). Those two writes are the whole story: file bodies in the CVMs are
plain, and the game resolves paths through `OGREDIR*.BIN`, never through
the scrambled ISO directories.

**Translation status.** The USA release is already English. The dialogue
files (`MEDIA/TALK/**/TALK_*.BIN`) contain no readable strings — text is
stored as glyph indices into the game's font sheets — so a text
export/import pair is the open piece of a re-translation pipeline; the
repacker, container walker and AVLZ compressor it would sit on are done.
See the open threads in [docs/FORMATS.md](docs/FORMATS.md).

### Audio and video

Voice, music and movies are stock CRI formats, so `media.py` only drives
ffmpeg — with one twist: 80% of the voice clips use an AHX header variant
ffmpeg refuses, which the tool relabels on the fly. Sound-effect banks are
the same `ps2_DTPK` container Xenosaga III uses, decoded with that kit's
pure-Python SPU-ADPCM decoder.

---

## Modules

| File | Role |
|---|---|
| `iso9660.py` | minimal ISO reader (shared with the Xenosaga I kit) |
| `cvm.py` | CRI ROFS header probe |
| `ogredir.py` | the `OGREDIR*.BIN` index → file list |
| `avlz.py` | AVLZ LZSS decoder + compatible encoder |
| `containers.py` | recursive `TLFD` container walker |
| `txbp.py` | texture packs → RGBA / PNG |
| `ome.py` | meshes → OBJ, bone helpers, wireframe debug renderer |
| `texbind.py` | binds a mesh's texture ids to the right sibling pack |
| `render.py` | numpy software rasteriser → textured preview PNGs |
| `blender_open.py` | assemble a character / folder and open it in Blender |
| `media.py` | ffmpeg-driven voice/music/movie conversion, DTPK SE banks |
| `repack.py` | write files and textures back into the ISO; verify |
| `cli.py`, `browse.py`, `gui.py` | the commands, and the local web GUI over them |

## How it was reversed

In one session against the retail disc: the CVM/ROFS layout and the
`OGREDIR` index from `MAIN.BIN`'s strings; AVLZ by brute-forcing LZSS
variants against the first directory block; the container format, TXBP
texture layout (including CSM1 palette order and mip chains) and the OME
mesh format (four vertex layouts, triangle strips, texture-id binding) from
the data itself, each verified by rendering. Details and remaining open
threads: [docs/FORMATS.md](docs/FORMATS.md).

## License

MIT — see [LICENSE](LICENSE). Yakuza is © SEGA; this project contains no
game assets.
