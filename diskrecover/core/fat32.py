"""Parser wolumenu FAT32 — parametry wyłącznie z BPB obrazu (nie z notatek)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from diskrecover.core.image import ImageReader


@dataclass(frozen=True)
class Fat32BPB:
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    num_fats: int
    root_entry_count: int  # 0 dla FAT32
    total_sectors_16: int
    media: int
    fat_size_16: int
    sectors_per_track: int
    num_heads: int
    hidden_sectors: int
    total_sectors_32: int
    fat_size_32: int
    ext_flags: int
    fs_version: int
    root_cluster: int
    fs_info: int
    backup_boot: int
    drive_number: int
    boot_signature: int
    volume_id: int
    volume_label: str
    fs_type: str
    oem_name: str
    jump_boot: bytes

    @property
    def cluster_size(self) -> int:
        return self.bytes_per_sector * self.sectors_per_cluster

    @property
    def total_sectors(self) -> int:
        return self.total_sectors_32 if self.total_sectors_32 else self.total_sectors_16

    @property
    def fat_size_sectors(self) -> int:
        return self.fat_size_32 if self.fat_size_32 else self.fat_size_16

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.bytes_per_sector not in (512, 1024, 2048, 4096):
            errors.append(f"Nieprawidłowy BytesPerSector: {self.bytes_per_sector}")
        if self.sectors_per_cluster not in (1, 2, 4, 8, 16, 32, 64, 128):
            errors.append(f"Nieprawidłowy SectorsPerCluster: {self.sectors_per_cluster}")
        if self.num_fats not in (1, 2):
            errors.append(f"Nieprawidłowa liczba FAT: {self.num_fats}")
        if self.reserved_sectors < 1:
            errors.append("ReservedSectors < 1")
        if self.fat_size_sectors < 1:
            errors.append("Rozmiar FAT < 1")
        if self.root_cluster < 2:
            errors.append(f"RootCluster < 2: {self.root_cluster}")
        if "FAT" not in self.fs_type.upper() and self.fat_size_16 != 0:
            # FAT32 ma fat_size_16=0; ostrzeżenie jeśli fs_type nie wygląda na FAT
            pass
        if self.fs_type.strip() and "FAT" not in self.fs_type.upper():
            errors.append(f"Podejrzany FSType: {self.fs_type!r}")
        return errors


def parse_bpb(boot: bytes) -> Fat32BPB:
    if len(boot) < 90:
        raise ValueError("Sektor boot za krótki na BPB FAT32")
    bps = int.from_bytes(boot[11:13], "little")
    spc = boot[13]
    reserved = int.from_bytes(boot[14:16], "little")
    num_fats = boot[16]
    root_ents = int.from_bytes(boot[17:19], "little")
    tot16 = int.from_bytes(boot[19:21], "little")
    media = boot[21]
    fat16 = int.from_bytes(boot[22:24], "little")
    spt = int.from_bytes(boot[24:26], "little")
    heads = int.from_bytes(boot[26:28], "little")
    hidden = int.from_bytes(boot[28:32], "little")
    tot32 = int.from_bytes(boot[32:36], "little")
    fat32 = int.from_bytes(boot[36:40], "little")
    ext_flags = int.from_bytes(boot[40:42], "little")
    fs_ver = int.from_bytes(boot[42:44], "little")
    root_cl = int.from_bytes(boot[44:48], "little")
    fs_info = int.from_bytes(boot[48:50], "little")
    backup = int.from_bytes(boot[50:52], "little")
    drive = boot[64]
    boot_sig = boot[66]
    vol_id = int.from_bytes(boot[67:71], "little")
    label = boot[71:82].decode("latin-1", errors="replace").strip("\x00 ")
    fs_type = boot[82:90].decode("latin-1", errors="replace").strip("\x00 ")
    oem = boot[3:11].decode("latin-1", errors="replace").strip("\x00 ")
    return Fat32BPB(
        bytes_per_sector=bps,
        sectors_per_cluster=spc,
        reserved_sectors=reserved,
        num_fats=num_fats,
        root_entry_count=root_ents,
        total_sectors_16=tot16,
        media=media,
        fat_size_16=fat16,
        sectors_per_track=spt,
        num_heads=heads,
        hidden_sectors=hidden,
        total_sectors_32=tot32,
        fat_size_32=fat32,
        ext_flags=ext_flags,
        fs_version=fs_ver,
        root_cluster=root_cl,
        fs_info=fs_info,
        backup_boot=backup,
        drive_number=drive,
        boot_signature=boot_sig,
        volume_id=vol_id,
        volume_label=label,
        fs_type=fs_type,
        oem_name=oem,
        jump_boot=boot[0:3],
    )


def looks_like_fat32_boot(boot: bytes) -> bool:
    if len(boot) < 90:
        return False
    if boot[510:512] != b"\x55\xaa":
        return False
    try:
        bpb = parse_bpb(boot)
    except Exception:
        return False
    return len(bpb.validate()) == 0 and bpb.fat_size_32 > 0


EOC_MIN = 0x0FFFFFF8
BAD_CLUSTER = 0x0FFFFFF7
FREE_CLUSTER = 0x00000000


@dataclass
class Fat32Volume:
    """Wolumen FAT32 nad obrazem — tylko odczyt."""

    reader: ImageReader
    partition_offset: int
    bpb: Fat32BPB
    _fat_cache: Dict[int, int] = field(default_factory=dict, repr=False)
    _fat_loaded: bool = field(default=False, repr=False)
    activity_cb: Optional[Callable[..., None]] = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        reader: ImageReader,
        partition_offset: int = 0,
        *,
        validate: bool = True,
    ) -> "Fat32Volume":
        # Spróbuj standardowego sektora, potem backup boot (sektor 6 typowo)
        boot = reader.read(partition_offset, 512)
        if not looks_like_fat32_boot(boot):
            # Spróbuj odczytać potencjalny backup
            for backup_sec in (6, 1):
                cand = reader.read(partition_offset + backup_sec * 512, 512)
                if looks_like_fat32_boot(cand):
                    boot = cand
                    break
        bpb = parse_bpb(boot)
        if validate:
            errs = bpb.validate()
            if errs:
                raise ValueError("BPB nie przeszło walidacji: " + "; ".join(errs))
        return cls(reader=reader, partition_offset=partition_offset, bpb=bpb)

    @property
    def cluster_size(self) -> int:
        return self.bpb.cluster_size

    @property
    def fat_offset(self) -> int:
        return self.partition_offset + self.bpb.reserved_sectors * self.bpb.bytes_per_sector

    @property
    def fat_bytes(self) -> int:
        return self.bpb.fat_size_sectors * self.bpb.bytes_per_sector

    @property
    def data_offset(self) -> int:
        # Po reserved + FATs; root w obszarze danych (FAT32)
        return self.fat_offset + self.bpb.num_fats * self.fat_bytes

    @property
    def total_clusters(self) -> int:
        data_sectors = self.bpb.total_sectors - (
            self.bpb.reserved_sectors + self.bpb.num_fats * self.bpb.fat_size_sectors
        )
        return data_sectors // self.bpb.sectors_per_cluster

    def cluster_offset(self, cluster: int) -> int:
        if cluster < 2:
            raise ValueError(f"Nieprawidłowy numer klastra: {cluster}")
        return self.data_offset + (cluster - 2) * self.cluster_size

    def read_cluster(self, cluster: int) -> bytes:
        data = self.reader.read(self.cluster_offset(cluster), self.cluster_size)
        if self.activity_cb:
            self.activity_cb(
                bytes_read=len(data),
                cluster=cluster,
                lba=self.cluster_offset(cluster) // self.bpb.bytes_per_sector,
            )
        return data

    def fat_entry(self, cluster: int) -> int:
        if cluster in self._fat_cache:
            return self._fat_cache[cluster]
        val = self.fat_entry_copy(0, cluster)
        self._fat_cache[cluster] = val
        return val

    def fat_entry_copy(self, copy_index: int, cluster: int) -> int:
        if copy_index < 0 or copy_index >= self.bpb.num_fats:
            raise ValueError(f"Nieprawidłowy indeks kopii FAT: {copy_index}")
        # FAT32: 4 bajty na wpis
        off = self.fat_offset + copy_index * self.fat_bytes + cluster * 4
        raw = self.reader.read(off, 4)
        if self.activity_cb:
            self.activity_cb(
                bytes_read=len(raw),
                cluster=cluster,
                lba=off // self.bpb.bytes_per_sector,
            )
        if len(raw) < 4:
            return FREE_CLUSTER
        val = int.from_bytes(raw, "little") & 0x0FFFFFFF
        return val

    def preload_fat(self, progress_cb=None, cancel_cb=None) -> None:
        """Załaduj tablicę FAT strumieniowo do cache (tylko odczyt)."""
        if self._fat_loaded:
            return
        entry_count = self.total_clusters + 2
        chunk = 64 * 1024  # 16k wpisów naraz
        bytes_needed = entry_count * 4
        loaded = 0
        for pos, data in self.reader.iter_chunks(self.fat_offset, bytes_needed, chunk):
            if cancel_cb and cancel_cb():
                raise InterruptedError("Anulowano wczytywanie FAT")
            base_idx = (pos - self.fat_offset) // 4
            for i in range(0, len(data) - 3, 4):
                idx = base_idx + i // 4
                if idx >= entry_count:
                    break
                val = int.from_bytes(data[i : i + 4], "little") & 0x0FFFFFFF
                self._fat_cache[idx] = val
            loaded += len(data)
            if progress_cb:
                progress_cb(min(100, int(100 * loaded / bytes_needed)))
        self._fat_loaded = True

    def follow_chain(
        self,
        start: int,
        max_clusters: Optional[int] = None,
        check_cb: Optional[Callable[[], None]] = None,
    ) -> List[int]:
        chain: List[int] = []
        seen = set()
        c = start
        limit = max_clusters if max_clusters is not None else self.total_clusters + 2
        while c >= 2 and c < EOC_MIN and c != BAD_CLUSTER:
            if check_cb:
                check_cb()
            if c in seen:
                if self.activity_cb:
                    self.activity_cb(corruptions=1, operation=f"Cykl FAT przy klastrze {c}")
                break  # pętla
            seen.add(c)
            chain.append(c)
            if len(chain) >= limit:
                break
            nxt = self.fat_entry(c)
            if nxt < 2 or nxt >= EOC_MIN or nxt == BAD_CLUSTER:
                break
            c = nxt
        return chain

    def is_allocated(self, cluster: int) -> bool:
        v = self.fat_entry(cluster)
        return v != FREE_CLUSTER and v != BAD_CLUSTER

    def is_eoc(self, value: int) -> bool:
        return value >= EOC_MIN

    def iter_allocated_clusters(self) -> Iterator[int]:
        for c in range(2, self.total_clusters + 2):
            if self.is_allocated(c):
                yield c

    def summary(self) -> dict:
        return {
            "partition_offset": self.partition_offset,
            "bytes_per_sector": self.bpb.bytes_per_sector,
            "sectors_per_cluster": self.bpb.sectors_per_cluster,
            "cluster_size": self.cluster_size,
            "reserved_sectors": self.bpb.reserved_sectors,
            "num_fats": self.bpb.num_fats,
            "fat_size_sectors": self.bpb.fat_size_sectors,
            "root_cluster": self.bpb.root_cluster,
            "total_sectors": self.bpb.total_sectors,
            "total_clusters": self.total_clusters,
            "volume_label": self.bpb.volume_label,
            "fs_type": self.bpb.fs_type,
            "oem_name": self.bpb.oem_name,
            "volume_id": f"{self.bpb.volume_id:08X}",
            "data_offset": self.data_offset,
            "fat_offset": self.fat_offset,
        }
