# System Prerequisites

This project requires two system-level dependencies that are **not pip-installable**. Install them before running:

## On Ubuntu/Debian (WSL or native):

```bash
sudo apt-get update
sudo apt-get install -y libreoffice poppler-utils
```

This installs:
- **LibreOffice** (`soffice`) — for rendering `.docx` files to PDF (document layout analysis)
- **poppler-utils** (`pdftoppm`) — for converting PDFs to images (visual inspection of rendered documents)

## Verification:

```bash
which soffice && echo "✓ LibreOffice installed" || echo "✗ LibreOffice NOT found"
which pdftoppm && echo "✓ poppler-utils installed" || echo "✗ poppler-utils NOT found"
```

Both must be present for modules that use the rendering pipeline (especially Module 8).
