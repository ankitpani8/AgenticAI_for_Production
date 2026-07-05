# System Prerequisites

This project requires two system-level dependencies that are **not pip-installable**. Both are needed for document rendering and PDF analysis (especially Module 8):

- **LibreOffice** (`soffice`) — renders `.docx` files to PDF for layout analysis
- **poppler-utils** (`pdftoppm`) — converts PDFs to images for visual inspection

Choose your platform below and install:

---

## Linux (Ubuntu/Debian, including WSL)

```bash
sudo apt-get update
sudo apt-get install -y libreoffice poppler-utils
```

---

## macOS

Using [Homebrew](https://brew.sh/):

```bash
brew install libreoffice poppler
```

(Homebrew's `poppler` package includes `pdftoppm`.)

---

## Windows (native)

**LibreOffice:**
1. Download the Windows installer from https://www.libreoffice.org/download/
2. Run the installer and complete setup
3. Add LibreOffice to PATH (usually `C:\Program Files\LibreOffice\program\`) or verify `soffice.exe` is accessible from PowerShell

**poppler-utils:**
- Option A (Chocolatey): `choco install poppler`
- Option B (vcpkg): `vcpkg install poppler:x64-windows`
- Option C (Pre-built binaries): Download from https://github.com/oschwartz10612/poppler-windows/releases/ and add `pdftoppm.exe` to PATH

---

## Verification (all platforms)

Run one of the following to confirm both tools are installed and accessible:

**macOS / Linux / WSL:**
```bash
which soffice && echo "✓ LibreOffice installed" || echo "✗ LibreOffice NOT found"
which pdftoppm && echo "✓ poppler-utils installed" || echo "✗ poppler-utils NOT found"
```

**Windows PowerShell:**
```powershell
(Get-Command soffice -ErrorAction SilentlyContinue) ? "✓ LibreOffice installed" : "✗ LibreOffice NOT found"
(Get-Command pdftoppm -ErrorAction SilentlyContinue) ? "✓ poppler-utils installed" : "✗ poppler-utils NOT found"
```

Both tools must be present and in your system PATH for the rendering pipeline to work.
