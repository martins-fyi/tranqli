# Build

```
pyinstaller --onefile --windowed --add-data "assets;assets" main.py
```

Output: `dist/main.exe` (~40–70 MB with Qt bundled).
Rename to `GreenTracker.exe` before distributing.
