"""Generator syntetycznych obrazów FAT32 do testów (bez prawdziwego pendrive)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


@dataclass
class Fat32Layout:
    bytes_per_sector: int = 512
    sectors_per_cluster: int = 4  # 2048 B klastra — wygodne do testów
    reserved_sectors: int = 32
    num_fats: int = 2
    fat_size_sectors: int = 32  # wystarczy na mały obraz
    root_cluster: int = 2
    total_sectors: int = 2048  # ~1 MB partycji
    partition_lba: int = 64  # jak „ok. 2904” — offset partycji w MBR


def _encode_sfn(name: str, is_dir: bool = False) -> bytes:
    """Prosty 8.3 — bez LFN."""
    if "." in name and not is_dir:
        base, _, ext = name.partition(".")
    else:
        base, ext = name, ""
    base = base.upper().replace(" ", "")[:8].ljust(8)
    ext = ext.upper().replace(" ", "")[:3].ljust(3)
    return (base + ext).encode("ascii")


def _dir_entry(
    name: str,
    cluster: int,
    size: int,
    *,
    is_dir: bool = False,
    deleted: bool = False,
    attr: Optional[int] = None,
) -> bytes:
    if attr is None:
        attr = 0x10 if is_dir else 0x20
    sfn = bytearray(_encode_sfn(name, is_dir=is_dir))
    if deleted:
        sfn[0] = 0xE5
    ent = bytearray(32)
    ent[0:11] = sfn
    ent[11] = attr
    ent[20:22] = (cluster >> 16).to_bytes(2, "little")
    ent[26:28] = (cluster & 0xFFFF).to_bytes(2, "little")
    ent[28:32] = size.to_bytes(4, "little")
    return bytes(ent)


def _lfn_entries(long_name: str, checksum: int) -> List[bytes]:
    """Buduj wpisy LFN (od końca)."""
    # UCS-2 znaki, 13 na wpis
    chars = list(long_name) + ["\x00"]
    # dopełnij 0xFFFF
    while len(chars) % 13 != 0:
        chars.append("\uffff")
    chunks = [chars[i : i + 13] for i in range(0, len(chars), 13)]
    entries: List[bytes] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks):
        seq = idx + 1
        if idx == total - 1:
            seq |= 0x40
        ent = bytearray(32)
        ent[0] = seq
        # name1
        for j in range(5):
            ch = ord(chunk[j]) if j < len(chunk) else 0xFFFF
            ent[1 + j * 2 : 3 + j * 2] = ch.to_bytes(2, "little")
        ent[11] = 0x0F
        ent[13] = checksum
        for j in range(6):
            ch = ord(chunk[5 + j]) if 5 + j < len(chunk) else 0xFFFF
            ent[14 + j * 2 : 16 + j * 2] = ch.to_bytes(2, "little")
        for j in range(2):
            ch = ord(chunk[11 + j]) if 11 + j < len(chunk) else 0xFFFF
            ent[28 + j * 2 : 30 + j * 2] = ch.to_bytes(2, "little")
        entries.append(bytes(ent))
    entries.reverse()  # na dysku: ostatni fragment pierwszy
    return entries


def _sfn_checksum(sfn: bytes) -> int:
    s = 0
    for b in sfn[:11]:
        s = ((s & 1) << 7) + (s >> 1) + b
        s &= 0xFF
    return s


def _dot_entries(self_cluster: int, parent_cluster: int) -> bytes:
    return _dir_entry(".", self_cluster, 0, is_dir=True) + _dir_entry(
        "..", parent_cluster if parent_cluster else 0, 0, is_dir=True
    )


def build_fat32_image(
    path: Path,
    *,
    layout: Optional[Fat32Layout] = None,
    with_mbr: bool = True,
    target_folder: str = "bhp szkoły",
    include_orphan: bool = True,
    include_deleted: bool = True,
    include_raw_payload: bool = True,
) -> dict:
    """
    Tworzy mały obraz z MBR + FAT32:
    - root/
      - BHPSZKOL/ (LFN: bhp szkoły)/
        - notatka.txt
        - foto.jpg (minimalny JPEG)
        - [opcjonalnie usunięty] stary.doc
      - INNE/inne.txt
    - orphan katalog (bez linku z root)
    - wolny obszar z PDF do carvingu
    """
    lay = layout or Fat32Layout()
    bps = lay.bytes_per_sector
    spc = lay.sectors_per_cluster
    cluster_size = bps * spc
    part_offset = lay.partition_lba * bps if with_mbr else 0

    # Rozmiar całego obrazu
    image_size = part_offset + lay.total_sectors * bps
    img = bytearray(image_size)

    # --- MBR ---
    if with_mbr:
        img[510:512] = b"\x55\xaa"
        # Partycja 0: FAT32 LBA
        ent = bytearray(16)
        ent[0] = 0x00
        ent[4] = 0x0C
        ent[8:12] = lay.partition_lba.to_bytes(4, "little")
        ent[12:16] = lay.total_sectors.to_bytes(4, "little")
        img[446:462] = ent

    # --- Boot sector ---
    boot = bytearray(bps)
    boot[0:3] = b"\xeb\x58\x90"
    boot[3:11] = b"MSDOS5.0"
    boot[11:13] = bps.to_bytes(2, "little")
    boot[13] = spc
    boot[14:16] = lay.reserved_sectors.to_bytes(2, "little")
    boot[16] = lay.num_fats
    boot[17:19] = (0).to_bytes(2, "little")
    boot[21] = 0xF8
    boot[24:26] = (63).to_bytes(2, "little")
    boot[26:28] = (255).to_bytes(2, "little")
    boot[28:32] = lay.partition_lba.to_bytes(4, "little")
    boot[32:36] = lay.total_sectors.to_bytes(4, "little")
    boot[36:40] = lay.fat_size_sectors.to_bytes(4, "little")
    boot[44:48] = lay.root_cluster.to_bytes(4, "little")
    boot[48:50] = (1).to_bytes(2, "little")  # FSInfo
    boot[50:52] = (6).to_bytes(2, "little")  # backup boot
    boot[64] = 0x80
    boot[66] = 0x29
    boot[67:71] = b"\x12\x34\x56\x78"
    boot[71:82] = b"DISKRECOVER"
    boot[82:90] = b"FAT32   "
    boot[510:512] = b"\x55\xaa"
    img[part_offset : part_offset + bps] = boot
    # backup boot @ reserved sector 6
    img[part_offset + 6 * bps : part_offset + 7 * bps] = boot

    fat_offset = part_offset + lay.reserved_sectors * bps
    fat_bytes = lay.fat_size_sectors * bps
    data_offset = fat_offset + lay.num_fats * fat_bytes

    def cluster_off(c: int) -> int:
        return data_offset + (c - 2) * cluster_size

    # Mapowanie klastrów:
    # 2: root
    # 3: bhp szkoły
    # 4: notatka.txt
    # 5: foto.jpg
    # 6: INNE/
    # 7: inne.txt
    # 8: orphan dir
    # 9: orphan file
    # 10: deleted file data (stary)
    # 11+: raw PDF area (marked free)

    fat = [0] * (lay.fat_size_sectors * bps // 4)
    fat[0] = 0x0FFFFFF8
    fat[1] = 0x0FFFFFFF

    def set_chain(clusters: List[int]) -> None:
        for a, b in zip(clusters, clusters[1:]):
            fat[a] = b
        fat[clusters[-1]] = 0x0FFFFFFF

    set_chain([2])
    set_chain([3])
    set_chain([4])
    set_chain([5])
    set_chain([6])
    set_chain([7])
    set_chain([8])
    set_chain([9])
    if include_deleted:
        set_chain([10])

    # Zapisz FAT x2
    fat_blob = bytearray(fat_bytes)
    for i, v in enumerate(fat):
        if i * 4 + 4 > fat_bytes:
            break
        fat_blob[i * 4 : i * 4 + 4] = (v & 0x0FFFFFFF).to_bytes(4, "little")
    for n in range(lay.num_fats):
        off = fat_offset + n * fat_bytes
        img[off : off + fat_bytes] = fat_blob

    # --- Zawartość ---
    notatka = "To jest notatka BHP ze szkolenia.\nTest DiskRecover.\n".encode("utf-8")
    # Minimalny JPEG
    jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xd9"
    )
    inne = b"Plik spoza katalogu docelowego.\n"
    orphan_file = b"Osierocony plik w osieroconym katalogu.\n"
    deleted_data = b"Usuniety dokument szkoleniowy."
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

    # root dir cluster 2
    root = bytearray(cluster_size)
    # LFN + SFN dla bhp szkoły → cluster 3
    sfn_bhp = _encode_sfn("BHPSZKOL", is_dir=True)
    lfn = _lfn_entries(target_folder, _sfn_checksum(sfn_bhp))
    pos = 0
    for e in lfn:
        root[pos : pos + 32] = e
        pos += 32
    root[pos : pos + 32] = _dir_entry("BHPSZKOL", 3, 0, is_dir=True)
    pos += 32
    root[pos : pos + 32] = _dir_entry("INNE", 6, 0, is_dir=True)
    img[cluster_off(2) : cluster_off(2) + cluster_size] = root

    # bhp szkoły cluster 3
    bhp = bytearray(cluster_size)
    bhp[0:64] = _dot_entries(3, 2)
    p = 64
    bhp[p : p + 32] = _dir_entry("NOTATKA.TXT", 4, len(notatka))
    p += 32
    bhp[p : p + 32] = _dir_entry("FOTO.JPG", 5, len(jpeg))
    p += 32
    if include_deleted:
        bhp[p : p + 32] = _dir_entry("STARY.DOC", 10, len(deleted_data), deleted=True)
    img[cluster_off(3) : cluster_off(3) + cluster_size] = bhp

    # files
    buf = bytearray(cluster_size)
    buf[: len(notatka)] = notatka
    img[cluster_off(4) : cluster_off(4) + cluster_size] = buf

    buf = bytearray(cluster_size)
    buf[: len(jpeg)] = jpeg
    img[cluster_off(5) : cluster_off(5) + cluster_size] = buf

    # INNE
    inne_dir = bytearray(cluster_size)
    inne_dir[0:64] = _dot_entries(6, 2)
    inne_dir[64:96] = _dir_entry("INNE.TXT", 7, len(inne))
    img[cluster_off(6) : cluster_off(6) + cluster_size] = inne_dir
    buf = bytearray(cluster_size)
    buf[: len(inne)] = inne
    img[cluster_off(7) : cluster_off(7) + cluster_size] = buf

    if include_orphan:
        od = bytearray(cluster_size)
        od[0:64] = _dot_entries(8, 2)
        od[64:96] = _dir_entry("ORPHAN.TXT", 9, len(orphan_file))
        img[cluster_off(8) : cluster_off(8) + cluster_size] = od
        buf = bytearray(cluster_size)
        buf[: len(orphan_file)] = orphan_file
        img[cluster_off(9) : cluster_off(9) + cluster_size] = buf

    if include_deleted:
        buf = bytearray(cluster_size)
        buf[: len(deleted_data)] = deleted_data
        img[cluster_off(10) : cluster_off(10) + cluster_size] = buf

    if include_raw_payload:
        # Umieść PDF w obszarze danych poza zaalokowanymi klastrami (klaster 20)
        raw_off = cluster_off(20)
        if raw_off + len(pdf) < len(img):
            img[raw_off : raw_off + len(pdf)] = pdf

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img)

    return {
        "path": str(path),
        "size": len(img),
        "partition_lba": lay.partition_lba if with_mbr else 0,
        "partition_offset": part_offset,
        "cluster_size": cluster_size,
        "root_cluster": lay.root_cluster,
        "target_folder": target_folder,
        "bytes_per_sector": bps,
        "sectors_per_cluster": spc,
    }


if __name__ == "__main__":
    out = Path(__file__).resolve().parent / "sample_fat32.bin"
    info = build_fat32_image(out)
    print(info)
