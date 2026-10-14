# Yakuza (PS2, USA, SLUS-21348) — disc and asset formats

Everything below was worked out from the retail disc image plus the game's
own executables (`SLUS_213.48` loader + `MAIN.BIN` overlay) in one sitting;
nothing here is copied from another tool. Byte offsets are little-endian
unless stated.

## 1. Disc layout

Plain ISO9660, single layer, 13 root files:

| File | Size | Role |
|---|---|---|
| `SYSTEM.CNF`, `IOPRP300.IMG`, `IRX.DAT` | small | boot config, IOP image, packed IRX modules |
| `SLUS_213.48` | 354 KB | tiny loader ELF (entry 0x100008) |
| `MAIN.BIN` | 4.3 MB | the actual game: a Metrowerks `MWo3` overlay loaded at 0x100000 (text 0x2e0d20, data 0x147420, bss 0x9b500) |
| `OGREDIR.BIN` / `OGRE.CVM` | 160 KB / 362 MB | `MEDIA`  — game data (maps, player, UI, text, SE) |
| `OGREDIR2.BIN` / `OGRE2.CVM` | 99 KB / 2.77 GB | `MEDIA2` — voice (AHX), BGM (ADX), movies (SFD), cutscene "author" data |
| `OGREDIR3.BIN` / `OGRE3.CVM` | 161 KB / 1.22 GB | `MEDIA3` — stage archives (`ARC/`) and enemy models (`ENEMY/`) |
| `OGREDIR4.BIN` / `OGRE4.CVM` | 1 KB / 4.4 MB | `MEDIA4` — minigame code modules + profile INIs |

The engine is CodeWarrior-built C++ (source names survive: `Media.cpp`,
`EventPlay*.cpp`, `Cse*.cpp`, `Effect*.cpp`), on CRI middleware: CVFS/ROFS
1.77 for the CVMs, ADX/AHX for audio, Sofdec (`SFD`) for movies, and the same
`ps2_DTPK` sound-bank container Xenosaga III uses.

## 2. CVM containers (CRI ROFS 1.52)

`CVMH` header, big-endian size at 0x20, `ROFSROFSBLD Ver.1.52 2003-06-09`,
then a standard ISO9660 image starting at 0x1800. Yakuza's four volumes are
**scrambled**: the PVD and every directory sector are encrypted (ROFS
password protection; the publicly known password is `qi2o@9a!`). File
bodies are stored in the clear, so we never touch the ISO directories —
the game ships its own index instead (§3). Byte offset of a file inside the
CVM is `0x1800 + lba * 2048`.

## 3. OGREDIR*.BIN — the directory index

The engine loads `%sOGREDIR%d.bin` at boot and resolves paths through it.

```
0x00  build timestamp bytes, zero padded
0x40  u32 directory count
0x44  u32 offset of the last AVLZ block
0x80  count × 64: { char name[0x38] ("/MEDIA/MAP/"), u32 entry_count, u32 block_offset }
...   AVLZ blocks (§4); each inflates to
        u32 entry_count, u32 entry_count, u32 dir_lba, "#DirLst#",
        entry_count × 48: { u32 0, u32 size, u32 0, u32 lba,
                            u8 flags (2 = directory), u8 name hash, char name[30] }
```

Totals: 15,612 files / 4.34 GB across the four volumes.

## 4. AVLZ — the engine's LZSS

Header `"AVLZ" | u32 decompressed size | u32 compressed size (counted from
the magic)`. Classic Okumura LZSS: 4096-byte zero-initialised ring buffer,
write cursor starts at 0xFEE, one flag byte per 8 tokens read LSB-first,
bit 1 = literal, bit 0 = two-byte match `lo, hi` with
`offset = lo | (hi & 0xF0) << 4`, `length = (hi & 0x0F) + 3`.
(`avlz.py`; a compatible compressor is included for repacking.)

## 5. The generic container (.DAT / .ARC / .BIN …)

Almost every data file is this recursive shape, nested up to ~5 deep:

```
0x00  u32 count
0x04  char tag[4] or 0        "TLFD", "EGTS", "ARTS" seen
0x08  8 bytes 0
0x10  count × { u32 offset (from container start), u32 size, u32 id, char tag[4] }
```

Zero-size entries are empty slots. Leaves found across the whole dump:

| Leaf | Count | Bytes | What |
|---|---|---|---|
| AVLZ → `OME\0` | 37,537 | 746 MB | meshes (§7) |
| AVLZ → `TXBP` | 27,561 | 993 MB | texture packs (§6) |
| `ps2_DTPK` | 2,817 | 207 MB | CRI sound banks (SE) |
| `Ver 1.x` | 7,953 | 5 MB | placement/property tables (text-ish) |
| `OSOS`, `OVRP`, `RELM`, `OCSP` | ~2.7k | 5 MB | stage object / visibility / collision records (open) |
| `0x80000000…` (`.B##`) | 1,905 | 616 MB | cutscene "author" streams (open) |

`.SGT` files (talk portraits) are a 0x20-byte `SGT\0` header followed by a
TXBP. `.OMT` / `.OM` are motion data (open). `.ICO` is a stock PS2
memory-card icon. `MEDIA4/MODULE/*.BIN` are more `MWo3` code overlays.

## 6. TXBP — texture packs

```
0x00  "TXBP"
0x04  u32 texture count
0x08  u32 largest payload (staging-buffer size)
0x20  for each texture: a 32-byte entry immediately followed by its payload
        u32 payload size (CLUT + pixels)
        u32 width
        u32 height            — or mip-level count when flags & 0x400 (square texture)
        u32 pixel format      0x14 = 4-bit indexed, 0x15 = 8-bit indexed, 0x1d = RGBA8888
        u32 CLUT format       0x1d, or 0xffffffff when none
        u32 flags             0x02000000 / 0x0a000000; bit 0x400 = mip chain
        u32 texture id        — what mesh sub-meshes reference
        u32 0xb26b4000|id     lookup key
      payload: CLUT (RGBA8, alpha 0..0x80, 8-bit CLUTs in GS CSM1 order:
      swap entries 8–15 ↔ 16–23 in every 32) then linear pixel indices
      (4-bit: low nibble first). Mip levels follow largest-first.
```

Pixels are stored linearly (not GS-swizzled); the CSM1 palette swap is
required for 8-bit textures. Verified visually on the player suit, street
asphalt and UI icons. Full-dump sweep: 83,179 PNGs; the only packs that do
not parse are five debug/test leaves (`MEDIA/DEBUG`, `EFFECT_TEST`, a
`0x1a` pixel format) and the older inline-CLUT layout of `MEDIA/DEBUG/ALL.TXB`.

## 7. OME — meshes

```
0x00  "OME\0"   0x04 u32 ?   0x08 u32 ODBP offset   0x10 u32 material rows
0x18  u32 format hint   0x30  rows × 32  material / GS-register table (opaque)

ODBP: "ODBP", u32 data offset, u32 n (unreliable), u32 total size, pad to 0x20
  +0x80  0x40-byte records until the data offset:
           u32 first-block offset (rel. ODBP+0x20), u32 material-colour offset,
           u32 texture id, i32, u32 id, 0, 0, u32 flags (low word 1 = sub-mesh),
           float centre xyz, float radius
         other record shapes in the same table are matrices and visibility
         cells { bbox, u16-list offset, count } that index a u16 block list
         sitting just before the first block.
  data:  blocks back-to-back, 16-byte aligned:
           u32 5, u32 vertex flags, u32 vertex offset, u32 vertex count,
           u32 index offset, u32 index count, 0, 0      (offsets rel. ODBP+0x20)
           vertices, then u16 triangle strip (degenerate restarts)
  tail:  per-sub-mesh colour records { RGBA ambient, diffuse, specular, 0, float shininess }
```

Vertex flags → components, in file order:

| bit | component | bytes |
|---|---|---|
| 0x0002 | position xyz | 12 |
| 0x0008 | position xyzw | 16 |
| 0x1000 | bone index (u32) | 4 |
| 0x0010 | normal xyz | 12 |
| 0x0040 | colour RGBA8 | 4 |
| 0x0100 | uv | 8 |

Observed: `0x142` (24 B, static maps), `0x112` (32 B, lit static),
`0x1116` (36 B), `0x1118` (40 B, skinned characters). Geometry is plain
floats — no VIF packets — so OBJ export is direct. A sub-mesh's texture id
is the `texture id` field of a TXBP entry in the same file (verified: the
street block's ids {3,4,5,8,9,10,13,15} are exactly its sibling pack).
Ids are only unique per sibling pack — id 0 means "this pack's one
texture" and recurs dozens of times in a stage ARC — and meshes and packs
sit in parallel containers (`/0/7/19/N` ↔ `/0/7/20/N`), so `texbind.py`
picks, per id, the pack whose container path is closest to the mesh.
Character files (`MEDIA/PLAYER`, `MEDIA3/ENEMY`, NPC sets) carry no usable
ids at all (every slot is 0): there, mesh index k pairs with pack index k,
and a mesh without a same-index pack takes the nearest unclaimed pack
(`texbind.bind_file`). That is how the 110-vertex decal mesh `/1/9` in
`MEDIA3/ENEMY/E333/E33300.DAT` gets pack `/0/5` — Kiryu's dragon back
tattoo, a separate 256² overlay on the shirtless body. Known gap: the
player head meshes are single-material but need face + hair sheets, so one
of the two is wrong in the export.

## 8. Audio / video (no decoder needed)

* `.AHX` — CRI AHX voice (4,813 files, `80 00 00 20`), `ffmpeg` decodes.
* `.ADX` — CRI ADX BGM (132 files), `ffmpeg` decodes.
* `.SFD` — Sofdec MPEG-1 PS + ADX (41 movies, 1.39 GB), `ffmpeg` decodes.
* `ps2_DTPK` SE banks — same container as Xenosaga III's `.dap`; that kit's
  decoder applies.

## 9. Open threads

* ROFS directory scramble (needed only to rebuild a CVM the game's own
  loader would accept; `MAIN.BIN` has `ROFS_GetScrambleInf` / `RFMAI_CancelScrambleG`).
* OME material table rows and bone/skeleton data for skinned meshes.
* `.OMT` motion, `.B##` author (cutscene) streams, `OSOS`/`OVRP`/`RELM`
  stage records, `Ver 1.x` placement tables, `TALK_*.BIN` message text.
