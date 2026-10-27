"""Parsowanie tablicy partycji MBR (tylko odczyt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from diskrecover.core.image import ImageReader


@dataclass(frozen=True)
class PartitionEntry:
    index: int
    bootable: bool
    type_id: int
    type_name: str
    lba_start: int
    sector_count: int

    @property
    def byte_offset(self) -> int:
        return self.lba_start * 512

    @property
    def byte_size(self) -> int:
        return self.sector_count * 512

    @property
    def is_fat32(self) -> bool:
        return self.type_id in (0x0B, 0x0C, 0x1B, 0x1C)


PARTITION_TYPES = {
    0x00: "Pusta",
    0x01: "FAT12",
    0x04: "FAT16 <32MB",
    0x05: "Extended",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    0x0B: "FAT32 CHS",
    0x0C: "FAT32 LBA",
    0x0E: "FAT16 LBA",
    0x0F: "Extended LBA",
    0x1B: "Hidden FAT32 CHS",
    0x1C: "Hidden FAT32 LBA",
    0x83: "Linux",
}


def parse_mbr(reader: ImageReader, sector_size: int = 512) -> List[PartitionEntry]:
    data = reader.read(0, sector_size)
    if len(data) < 512:
        raise ValueError("Obraz za mały na sektor MBR")
    if data[510:512] != b"\x55\xaa":
        # Brak sygnatury — może być surowy wolumin bez MBR
        return []

    partitions: List[PartitionEntry] = []
    for i in range(4):
        off = 446 + i * 16
        entry = data[off : off + 16]
        type_id = entry[4]
        lba = int.from_bytes(entry[8:12], "little")
        count = int.from_bytes(entry[12:16], "little")
        if type_id == 0 and lba == 0 and count == 0:
            continue
        partitions.append(
            PartitionEntry(
                index=i,
                bootable=bool(entry[0] & 0x80),
                type_id=type_id,
                type_name=PARTITION_TYPES.get(type_id, f"0x{type_id:02X}"),
                lba_start=lba,
                sector_count=count,
            )
        )
    return partitions


def find_fat32_partition(
    reader: ImageReader, preferred_lba: Optional[int] = None
) -> Optional[PartitionEntry]:
    """Znajdź partycję FAT32. Preferuj preferred_lba jeśli podane i pasuje."""
    parts = parse_mbr(reader)
    fat_parts = [p for p in parts if p.is_fat32]
    if preferred_lba is not None:
        for p in fat_parts:
            if p.lba_start == preferred_lba:
                return p
        # Preferowany LBA może nie mieć typu FAT32 w MBR — sprawdź BPB później
        for p in parts:
            if p.lba_start == preferred_lba:
                return p
    if fat_parts:
        return fat_parts[0]
    return parts[0] if parts else None
