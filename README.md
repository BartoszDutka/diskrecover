# DiskRecover

Bezpieczne odzyskiwanie danych z obrazów FAT32 — aplikacja desktopowa dla **Bartosza Dutki** (Windows 11, polski interfejs).

Przeznaczona do rekonstrukcji uszkodzonego katalogu **`bhp szkoły`** z obrazu pendrive (np. `pendrive_bhp.bin`).

## Ważne zasady

- **Tylko odczyt** — obraz i tablice FAT **nigdy** nie są modyfikowane.
- Parametry partycji/BPB (sektor, klaster, FAT, root) są **odczytywane i walidowane z obrazu**, nie z notatek.
- Pliki bez pewnego pochodzenia → `Nieustalone_pochodzenie/`.
- Wyniki RAW (carving po sygnaturach) **nigdy** nie są przypisywane do `bhp szkoły` bez dowodu.
- Duże obrazy (~16 GB) są czytane strumieniowo (mmap / chunki) — bez ładowania całości do RAM.

## Wymagania

- Python 3.10+
- Windows 11 (GUI) lub Linux (CLI + testy)

```bash
pip install -r requirements.txt
```

## Uruchomienie

### GUI (domyślnie)

```bash
python -m diskrecover
# lub
python -m diskrecover gui
```

### CLI — analiza BPB/MBR

```bash
python -m diskrecover analyze "C:\Users\bdutka\Desktop\Odzyskiwanie_BHP\pendrive_bhp.bin"
```

### CLI — pełne odzyskiwanie

```bash
python -m diskrecover recover "C:\Users\bdutka\Desktop\Odzyskiwanie_BHP\pendrive_bhp.bin" -o "D:\Odzysk_BHP" --target "bhp szkoły"
```

Opcje: `--no-raw`, `--shallow`, `--partition-lba N`, `--json`.

## Układ katalogu wyjściowego

```
Odtworzony_bhp_szkoly/     — pliki z dowodem przynależności do katalogu docelowego
Osierocone_katalogi/       — katalogi wykryte lokalnie, bez linku z drzewa
RAW/                       — carving po sygnaturach (PDF, Office, obrazy, media…)
Nieustalone_pochodzenie/   — niepewna przynależność
Raport/                    — raport.html, raport.json, raport.csv, checkpoint.json
```

## Testy

Testy używają **syntetycznych obrazów FAT32** — nie potrzeba prawdziwego pendrive.

```bash
pytest -q
```

### Diagnostyka długiego skanowania

Podczas odzyskiwania aplikacja wykonuje rzeczywisty skan w workerze `QThread`.
Zakładka „Skanowanie” pokazuje liczniki odczytów obrazu, klastrów, wpisów
katalogowych i uszkodzeń FAT. Pasek etapu może pozostać nieokreślony, gdy
liczby operacji nie da się wiarygodnie wyznaczyć — aplikacja nie udaje wtedy
procentowego postępu.

Po rozpoczęciu skanowania tworzony jest:

```text
<katalog-wyjściowy>\Raport\scanner_debug.log
```

Log jest zapisywany na bieżąco. Brak heartbeat przez 30 sekund powoduje
ostrzeżenie diagnostyczne, ale nie kończy ani nie oznacza automatycznie procesu
jako zawieszonego. Łańcuchy FAT mają limit wynikający z liczby klastrów
woluminu oraz zbiór odwiedzonych klastrów, więc cykliczne referencje są
przerywane.

## Budowa .exe (PyInstaller)

Na Windows:

```bash
pip install pyinstaller
python scripts/build_exe.py
```

## Kontekst problemu (domyślne wartości w UI)

| Parametr | Przykład użytkownika | Uwaga |
|----------|----------------------|--------|
| Obraz | `C:\Users\bdutka\Desktop\Odzyskiwanie_BHP\pendrive_bhp.bin` | ~15 560 867 840 B |
| Sektor | 512 | Walidowane z BPB |
| MBR / LBA | ~2904 | Odczyt z MBR; można nadpisać w UI |
| Klaster | raportowano 8192 | **Zawsze z BPB obrazu** |
| Cel | `bhp szkoły` | |

## Nieobsługiwane

- Zapis / naprawa FAT in-place na obrazie lub nośniku
- Systemy plików inne niż FAT32
- Montowanie obrazu jako dysku
- Gwarancja 100% odzyskania przy całkowicie zniszczonych metadanych

## Licencja / autor

Narzędzie przygotowane na potrzeby odzyskania danych Bartosza Dutki.
